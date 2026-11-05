"""Read-only adapter for gitlab.cern.ch.

Covers the retained tools of the reference ``cerngitlab-mcp`` server: project
search, project detail, repository tree and file reads, README lookup, code /
issue search, merge requests, wikis, branches, releases, and tags via the
GitLab REST API v4. All interactions are GET-only.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from cmsmcp.adapters.common import json_body
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, UpstreamError
from cmsmcp.contracts.results import SearchResult
from cmsmcp.net.client import SafeHttpClient

SOURCE = "gitlab"

BINARY_EXTENSIONS = {
    ".root",
    ".h5",
    ".npy",
    ".png",
    ".jpg",
    ".gif",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".whl",
    ".so",
}

README_CANDIDATES = (
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "readme.md",
    "readme.rst",
    "readme.txt",
    "Readme.md",
)

_README_FORMATS = {
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".txt": "plaintext",
}

CODE_SCOPES = ("blobs", "filenames")
ISSUE_STATES = ("opened", "closed", "all")
TAG_SORTS = ("asc", "desc")
VISIBILITIES = ("public", "internal", "private")
MR_STATES = ("opened", "closed", "locked", "merged", "all")
MR_ORDER_BY = ("created_at", "updated_at", "title", "merged_at")
PIPELINE_STATUSES = (
    "created",
    "waiting_for_resource",
    "preparing",
    "pending",
    "running",
    "success",
    "failed",
    "canceled",
    "skipped",
    "manual",
    "scheduled",
)
JOB_SCOPES = (
    "created",
    "pending",
    "running",
    "failed",
    "success",
    "canceled",
    "skipped",
    "waiting_for_resource",
    "manual",
)
_TRACE_TEXT_LIMIT = 60000
_NOT_FOUND = 404
_MAX_PER_PAGE = 100


def _url(settings: Settings, path: str) -> str:
    return f"{settings.gitlab_base_url.rstrip('/')}/api/v4{path}"


def _service_headers(settings: Settings) -> dict[str, str] | None:
    if settings.gitlab_token:
        return {"PRIVATE-TOKEN": settings.gitlab_token}
    return None


def _project_key(project: str) -> str:
    text = str(project)
    if text.isdigit():
        return text
    return quote(text, safe="")


def _clamp_per_page(per_page: int) -> int:
    return min(max(per_page, 1), _MAX_PER_PAGE)


def _page_result(
    items: list[dict[str, Any]],
    *,
    per_page: int,
    page: int,
    query: str | None = None,
    sort: str | None = None,
    note: str | None = None,
) -> SearchResult:
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=None,
        page=page,
        size=per_page,
        has_more=len(items) == per_page,
        query=query,
        sort=sort,
        note=note,
    )


def _project_item(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "path_with_namespace": project.get("path_with_namespace"),
        "description": project.get("description") or None,
        "web_url": project.get("web_url"),
        "default_branch": project.get("default_branch"),
        "topics": project.get("topics") or [],
        "star_count": project.get("star_count"),
        "forks_count": project.get("forks_count"),
        "last_activity_at": project.get("last_activity_at"),
        "created_at": project.get("created_at"),
        "visibility": project.get("visibility"),
    }


def _commit_item(commit: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": commit.get("id"),
        "short_id": commit.get("short_id"),
        "title": commit.get("title"),
        "author_name": commit.get("author_name"),
        "created_at": commit.get("created_at"),
        "committed_date": commit.get("committed_date"),
        "web_url": commit.get("web_url"),
    }


async def search_projects(
    client: SafeHttpClient,
    settings: Settings,
    *,
    query: str | None = None,
    language: str | None = None,
    topic: str | None = None,
    visibility: str | None = None,
    sort_by: str = "last_activity_at",
    order: str = "desc",
    per_page: int = 20,
    page: int = 1,
) -> SearchResult:
    """Search projects on gitlab.cern.ch visible to the authenticated identity."""
    if visibility is not None and visibility not in VISIBILITIES:
        raise InvalidInputError(
            f"visibility must be one of {VISIBILITIES}, got {visibility!r}",
            instructions="Use visibility='public', 'internal', or 'private'.",
        )
    per_page = _clamp_per_page(per_page)
    params: dict[str, str | int] = {
        "order_by": sort_by,
        "sort": order,
        "per_page": per_page,
        "page": page,
    }
    if visibility:
        params["visibility"] = visibility
    if query:
        params["search"] = query
    if language:
        params["with_programming_language"] = language
    if topic:
        params["topic"] = topic
    response = await client.request(
        "GET",
        _url(settings, "/projects"),
        params=params,
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items = [_project_item(project) for project in data]
    return _page_result(
        items,
        per_page=per_page,
        page=page,
        query=query,
        sort=f"{sort_by}:{order}",
    )


async def get_project(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
) -> dict[str, Any]:
    """Return full metadata for one project, with statistics and languages."""
    key = _project_key(project)
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{key}"),
        params={"statistics": "true"},
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    languages_response = await client.request(
        "GET",
        _url(settings, f"/projects/{key}/languages"),
        service_headers=_service_headers(settings),
    )
    languages = json_body(languages_response, source=SOURCE)
    statistics = data.get("statistics") or {}
    license_info = data.get("license") or {}
    namespace = data.get("namespace") or {}
    item = _project_item(data)
    item.update(
        {
            "source": SOURCE,
            "path": data.get("path"),
            "ssh_url_to_repo": data.get("ssh_url_to_repo"),
            "http_url_to_repo": data.get("http_url_to_repo"),
            "statistics": {
                "commit_count": statistics.get("commit_count"),
                "repository_size": statistics.get("repository_size"),
                "storage_size": statistics.get("storage_size"),
            },
            "languages": languages if isinstance(languages, dict) else {},
            "license": license_info.get("name"),
            "namespace": {
                "name": namespace.get("name"),
                "path": namespace.get("path"),
            },
        }
    )
    return item


async def list_files(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    path: str | None = None,
    ref: str | None = None,
    recursive: bool = False,
    per_page: int = 100,
    page: int = 1,
) -> SearchResult:
    """List entries of a repository tree."""
    per_page = _clamp_per_page(per_page)
    params: dict[str, str | int] = {
        "recursive": "true" if recursive else "false",
        "per_page": per_page,
        "page": page,
    }
    if path:
        params["path"] = path
    if ref:
        params["ref"] = ref
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/repository/tree"),
        params=params,
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items = [
        {
            "name": entry.get("name"),
            "type": entry.get("type"),
            "path": entry.get("path"),
            "mode": entry.get("mode"),
        }
        for entry in data
    ]
    return _page_result(items, per_page=per_page, page=page, note=f"project={project}")


async def get_file(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    file_path: str,
    ref: str | None = None,
) -> dict[str, Any]:
    """Fetch and decode one repository file."""
    key = _project_key(project)
    resolved_ref = ref
    if not resolved_ref:
        project_response = await client.request(
            "GET",
            _url(settings, f"/projects/{key}"),
            service_headers=_service_headers(settings),
        )
        project_data = json_body(project_response, source=SOURCE)
        resolved_ref = project_data.get("default_branch") or "main"
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{key}/repository/files/{quote(file_path, safe='')}"),
        params={"ref": resolved_ref},
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    raw = base64.b64decode(data.get("content") or b"")
    extension = "." + file_path.rsplit(".", 1)[1].lower() if "." in file_path else ""
    is_binary = b"\x00" in raw or extension in BINARY_EXTENSIONS
    if is_binary:
        content = f"[Binary file, {len(raw)} bytes]"
    else:
        content = raw.decode("utf-8", errors="replace")
    return {
        "file_name": data.get("file_name"),
        "file_path": data.get("file_path"),
        "size": data.get("size"),
        "ref": data.get("ref") or resolved_ref,
        "last_commit_id": data.get("last_commit_id"),
        "content_sha256": data.get("content_sha256"),
        "is_binary": is_binary,
        "content": content,
        "language": None,
    }


async def get_readme(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    ref: str | None = None,
) -> dict[str, Any]:
    """Find and return the README of a project by probing candidate names."""
    tried: list[str] = []
    for candidate in README_CANDIDATES:
        tried.append(candidate)
        try:
            fetched = await get_file(
                client, settings, project=project, file_path=candidate, ref=ref
            )
        except UpstreamError as exc:
            if getattr(exc, "status_code", None) == _NOT_FOUND:
                continue
            raise
        extension = "." + candidate.rsplit(".", 1)[1].lower() if "." in candidate else ""
        return {
            "found": True,
            "tried": tried,
            "file_name": fetched["file_name"],
            "file_path": fetched["file_path"],
            "ref": fetched["ref"],
            "size": fetched["size"],
            "content": fetched["content"],
            "format": _README_FORMATS.get(extension, "markdown"),
        }
    return {"found": False, "tried": tried, "content": None}


async def search_code(
    client: SafeHttpClient,
    settings: Settings,
    *,
    search_term: str,
    project: str | None = None,
    scope: str = "blobs",
    ref: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> SearchResult:
    """Search code blobs or file names, globally or within one project."""
    if scope not in CODE_SCOPES:
        raise InvalidInputError(
            f"scope must be one of {CODE_SCOPES}, got {scope!r}",
            instructions="Use scope='blobs' for content search or 'filenames' for file names.",
        )
    per_page = _clamp_per_page(per_page)
    params: dict[str, str | int] = {
        "search": search_term,
        "scope": scope,
        "per_page": per_page,
        "page": page,
    }
    if ref:
        params["ref"] = ref
    if project:
        path = f"/projects/{_project_key(project)}/search"
    else:
        path = "/search"
    response = await client.request(
        "GET",
        _url(settings, path),
        params=params,
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items: list[dict[str, Any]] = []
    for hit in data:
        item = {
            "file_name": hit.get("filename") or hit.get("basename"),
            "file_path": hit.get("path"),
            "project_id": hit.get("project_id"),
            "ref": hit.get("ref"),
        }
        if scope == "blobs":
            item["data"] = hit.get("data")
            item["startline"] = hit.get("startline")
        items.append(item)
    return _page_result(
        items,
        per_page=per_page,
        page=page,
        query=search_term,
        note=f"scope={scope}" if project is None else f"scope={scope} project={project}",
    )


async def search_issues(
    client: SafeHttpClient,
    settings: Settings,
    *,
    search_term: str,
    project: str | None = None,
    state: str = "all",
    per_page: int = 10,
    page: int = 1,
) -> SearchResult:
    """Search issues globally or within one project."""
    if state not in ISSUE_STATES:
        raise InvalidInputError(
            f"state must be one of {ISSUE_STATES}, got {state!r}",
            instructions="Use state='opened', 'closed', or 'all'.",
        )
    per_page = _clamp_per_page(per_page)
    params: dict[str, str | int] = {
        "search": search_term,
        "scope": "all",
        "state": state,
        "per_page": per_page,
        "page": page,
    }
    if project:
        path = f"/projects/{_project_key(project)}/issues"
    else:
        path = "/issues"
    response = await client.request(
        "GET",
        _url(settings, path),
        params=params,
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items = []
    for issue in data:
        description = issue.get("description") or ""
        author = issue.get("author") or {}
        items.append(
            {
                "id": issue.get("id"),
                "iid": issue.get("iid"),
                "title": issue.get("title"),
                "description_snippet": description[:200],
                "state": issue.get("state"),
                "web_url": issue.get("web_url"),
                "author": author.get("username"),
                "created_at": issue.get("created_at"),
            }
        )
    return _page_result(
        items,
        per_page=per_page,
        page=page,
        query=search_term,
        note=f"state={state}" if project is None else f"state={state} project={project}",
    )


async def list_merge_requests(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    state: str = "opened",
    order_by: str = "created_at",
    sort: str = "desc",
    search: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> SearchResult:
    """List merge requests of one project."""
    if state not in MR_STATES:
        raise InvalidInputError(
            f"state must be one of {MR_STATES}, got {state!r}",
            instructions="Use state='opened', 'closed', 'locked', 'merged', or 'all'.",
        )
    if order_by not in MR_ORDER_BY:
        raise InvalidInputError(
            f"order_by must be one of {MR_ORDER_BY}, got {order_by!r}",
            instructions="Use order_by='created_at', 'updated_at', 'title', or 'merged_at'.",
        )
    if sort not in TAG_SORTS:
        raise InvalidInputError(
            f"sort must be one of {TAG_SORTS}, got {sort!r}",
            instructions="Use sort='asc' or sort='desc'.",
        )
    per_page = _clamp_per_page(per_page)
    params: dict[str, str | int] = {
        "state": state,
        "order_by": order_by,
        "sort": sort,
        "per_page": per_page,
        "page": page,
    }
    if search:
        params["search"] = search
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/merge_requests"),
        params=params,
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items = []
    for merge_request in data:
        description = merge_request.get("description") or ""
        author = merge_request.get("author") or {}
        items.append(
            {
                "id": merge_request.get("id"),
                "iid": merge_request.get("iid"),
                "title": merge_request.get("title"),
                "description_snippet": description[:200],
                "state": merge_request.get("state"),
                "draft": bool(merge_request.get("draft", False)),
                "source_branch": merge_request.get("source_branch"),
                "target_branch": merge_request.get("target_branch"),
                "web_url": merge_request.get("web_url"),
                "author": author.get("username"),
                "created_at": merge_request.get("created_at"),
                "updated_at": merge_request.get("updated_at"),
                "merge_status": merge_request.get("merge_status"),
            }
        )
    return _page_result(
        items,
        per_page=per_page,
        page=page,
        query=search,
        sort=f"{order_by}:{sort}",
        note=f"state={state} project={project}",
    )


async def get_wiki(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    page_slug: str | None = None,
) -> dict[str, Any]:
    """List wiki pages of a project or fetch one page with content."""
    key = _project_key(project)
    if page_slug is None:
        response = await client.request(
            "GET",
            _url(settings, f"/projects/{key}/wikis"),
            params={"with_content": "false"},
            service_headers=_service_headers(settings),
        )
        pages = json_body(response, source=SOURCE)
        return {
            "mode": "list",
            "project": project,
            "total_pages": len(pages),
            "pages": [
                {
                    "title": entry.get("title"),
                    "slug": entry.get("slug"),
                    "format": entry.get("format"),
                }
                for entry in pages
            ],
        }
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{key}/wikis/{quote(page_slug, safe='')}"),
        service_headers=_service_headers(settings),
    )
    page_data = json_body(response, source=SOURCE)
    return {
        "mode": "detail",
        "project": project,
        "title": page_data.get("title"),
        "slug": page_data.get("slug"),
        "format": page_data.get("format"),
        "content": page_data.get("content"),
    }


async def list_branches(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    search: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> SearchResult:
    """List branches of a project repository."""
    per_page = _clamp_per_page(per_page)
    params: dict[str, str | int] = {"per_page": per_page, "page": page}
    if search:
        params["search"] = search
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/repository/branches"),
        params=params,
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items = [
        {
            "name": branch.get("name"),
            "default": bool(branch.get("default", False)),
            "merged": bool(branch.get("merged", False)),
            "protected": bool(branch.get("protected", False)),
            "web_url": branch.get("web_url"),
            "commit": _commit_item(branch.get("commit") or {}),
        }
        for branch in data
    ]
    return _page_result(
        items, per_page=per_page, page=page, query=search, note=f"project={project}"
    )


async def list_releases(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    per_page: int = 20,
    page: int = 1,
) -> SearchResult:
    """List releases of a project."""
    per_page = _clamp_per_page(per_page)
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/releases"),
        params={"per_page": per_page, "page": page},
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items = []
    for release in data:
        author = release.get("author") or {}
        commit = release.get("commit") or {}
        assets = release.get("assets") or {}
        items.append(
            {
                "tag_name": release.get("tag_name"),
                "name": release.get("name"),
                "description": release.get("description") or None,
                "created_at": release.get("created_at"),
                "released_at": release.get("released_at"),
                "author": author.get("username"),
                "commit_path": commit.get("short_id"),
                "assets_count": len(assets.get("links") or []),
                "sources_count": len(assets.get("sources") or []),
            }
        )
    return _page_result(items, per_page=per_page, page=page, note=f"project={project}")


async def get_release(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    tag_name: str,
) -> dict[str, Any]:
    """Return one release by tag name."""
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/releases/{quote(tag_name, safe='')}"),
        service_headers=_service_headers(settings),
    )
    release = json_body(response, source=SOURCE)
    author = release.get("author") or {}
    commit = release.get("commit") or {}
    assets = release.get("assets") or {}
    return {
        "project": project,
        "tag_name": release.get("tag_name"),
        "name": release.get("name"),
        "description": release.get("description") or None,
        "created_at": release.get("created_at"),
        "released_at": release.get("released_at"),
        "author": author.get("username"),
        "commit": {
            "id": commit.get("id"),
            "short_id": commit.get("short_id"),
            "title": commit.get("title"),
            "created_at": commit.get("created_at"),
            "author_name": commit.get("author_name"),
        },
        "assets": {
            "links": [
                {
                    "name": link.get("name"),
                    "url": link.get("url"),
                    "link_type": link.get("link_type"),
                }
                for link in assets.get("links") or []
            ],
            "sources": [
                {"format": source.get("format"), "url": source.get("url")}
                for source in assets.get("sources") or []
            ],
        },
    }


async def list_tags(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    search: str | None = None,
    sort: str = "desc",
    per_page: int = 20,
    page: int = 1,
) -> SearchResult:
    """List tags of a project repository."""
    if sort not in TAG_SORTS:
        raise InvalidInputError(
            f"sort must be one of {TAG_SORTS}, got {sort!r}",
            instructions="Use sort='asc' or sort='desc'.",
        )
    per_page = _clamp_per_page(per_page)
    params: dict[str, str | int] = {"sort": sort, "per_page": per_page, "page": page}
    if search:
        params["search"] = search
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/repository/tags"),
        params=params,
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items = []
    for tag in data:
        commit = tag.get("commit") or {}
        items.append(
            {
                "name": tag.get("name"),
                "message": tag.get("message") or None,
                "target": tag.get("target"),
                "commit": {
                    "id": commit.get("id"),
                    "short_id": commit.get("short_id"),
                    "title": commit.get("title"),
                    "created_at": commit.get("created_at"),
                    "author_name": commit.get("author_name"),
                },
                "protected": bool(tag.get("protected", False)),
            }
        )
    return _page_result(
        items, per_page=per_page, page=page, query=search, sort=sort, note=f"project={project}"
    )


def _pipeline_item(pipeline: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": pipeline.get("id"),
        "iid": pipeline.get("iid"),
        "status": pipeline.get("status"),
        "ref": pipeline.get("ref"),
        "sha": pipeline.get("sha"),
        "source": pipeline.get("source"),
        "created_at": pipeline.get("created_at"),
        "updated_at": pipeline.get("updated_at"),
        "web_url": pipeline.get("web_url"),
    }


def _job_item(job: dict[str, Any]) -> dict[str, Any]:
    user = job.get("user") or {}
    runner = job.get("runner") or {}
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "stage": job.get("stage"),
        "status": job.get("status"),
        "ref": job.get("ref"),
        "tag": bool(job.get("tag", False)),
        "allow_failure": bool(job.get("allow_failure", False)),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "duration": job.get("duration"),
        "queued_duration": job.get("queued_duration"),
        "web_url": job.get("web_url"),
        "user": user.get("username"),
        "runner": runner.get("description"),
    }


async def list_pipelines(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    status: str | None = None,
    ref: str | None = None,
    source: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> SearchResult:
    """List pipelines of one project, optionally filtered by status/ref/source."""
    if status is not None and status not in PIPELINE_STATUSES:
        raise InvalidInputError(
            f"status must be one of {PIPELINE_STATUSES}, got {status!r}",
            instructions="Omit status for every pipeline state.",
        )
    per_page = _clamp_per_page(per_page)
    params: dict[str, str | int] = {"per_page": per_page, "page": page}
    if status:
        params["status"] = status
    if ref:
        params["ref"] = ref
    if source:
        params["source"] = source
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/pipelines"),
        params=params,
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items = [_pipeline_item(pipeline) for pipeline in data]
    return _page_result(
        items,
        per_page=per_page,
        page=page,
        note=f"project={project}",
    )


async def get_pipeline(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    pipeline_id: int,
) -> dict[str, Any]:
    """Return full detail of one pipeline of one project."""
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/pipelines/{int(pipeline_id)}"),
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    item = _pipeline_item(data)
    user = data.get("user") or {}
    item.update(
        {
            "before_sha": data.get("before_sha"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "committed_at": data.get("committed_at"),
            "duration": data.get("duration"),
            "queued_duration": data.get("queued_duration"),
            "coverage": data.get("coverage"),
            "user": user.get("username"),
        }
    )
    return item


async def list_pipeline_jobs(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    pipeline_id: int,
    scope: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> SearchResult:
    """List jobs of one pipeline, optionally filtered to one status scope."""
    if scope is not None and scope not in JOB_SCOPES:
        raise InvalidInputError(
            f"scope must be one of {JOB_SCOPES}, got {scope!r}",
            instructions="Omit scope for every job state.",
        )
    per_page = _clamp_per_page(per_page)
    params: dict[str, str | int] = {"per_page": per_page, "page": page}
    if scope:
        params["scope"] = scope
    response = await client.request(
        "GET",
        _url(
            settings,
            f"/projects/{_project_key(project)}/pipelines/{int(pipeline_id)}/jobs",
        ),
        params=params,
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    items = [_job_item(job) for job in data]
    return _page_result(
        items,
        per_page=per_page,
        page=page,
        note=f"project={project} pipeline={pipeline_id}",
    )


async def get_job(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    job_id: int,
) -> dict[str, Any]:
    """Return full detail of one job of one project."""
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/jobs/{int(job_id)}"),
        service_headers=_service_headers(settings),
    )
    data = json_body(response, source=SOURCE)
    item = _job_item(data)
    pipeline = data.get("pipeline") or {}
    commit = data.get("commit") or {}
    item.update(
        {
            "pipeline": {
                "id": pipeline.get("id"),
                "status": pipeline.get("status"),
                "ref": pipeline.get("ref"),
                "sha": pipeline.get("sha"),
                "web_url": pipeline.get("web_url"),
            },
            "commit": {
                "id": commit.get("id"),
                "short_id": commit.get("short_id"),
                "title": commit.get("title"),
            },
            "artifacts": [
                {
                    "file_type": artifact.get("file_type"),
                    "filename": artifact.get("filename"),
                    "size": artifact.get("size"),
                }
                for artifact in data.get("artifacts") or []
                if isinstance(artifact, dict)
            ],
        }
    )
    return item


async def get_job_trace(
    client: SafeHttpClient,
    settings: Settings,
    *,
    project: str,
    job_id: int,
    tail_chars: int = _TRACE_TEXT_LIMIT,
) -> dict[str, Any]:
    """Return the plain-text log (trace) of one job, tail-truncated when large."""
    response = await client.request(
        "GET",
        _url(settings, f"/projects/{_project_key(project)}/jobs/{int(job_id)}/trace"),
        service_headers=_service_headers(settings),
    )
    text = response.content.decode("utf-8", errors="replace")
    total_chars = len(text)
    truncated = total_chars > tail_chars
    if truncated:
        text = text[-tail_chars:]
    return {
        "project": project,
        "job_id": int(job_id),
        "total_chars": total_chars,
        "truncated": truncated,
        "trace": text,
    }
