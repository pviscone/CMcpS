"""Read-only adapter for CERN Jira (its.cern.ch/jira).

Endpoint type: standard Jira REST API v2 under ``/jira/rest/api/2``:

- ``GET /rest/api/2/serverInfo`` — anonymous (verified).
- ``GET /rest/api/2/search?jql=...&startAt=..&maxResults=..&fields=..`` —
  anonymous for public projects (verified); restricted projects/issues
  require CERN SSO, which the shared client injects.
- ``GET /rest/api/2/issue/<KEY>?fields=..`` — issue detail; description is
  plain text (Jira Server), attachments listed with content URLs.
- ``GET /rest/api/2/project/<KEY>`` — project metadata.

Envelope: search returns ``{issues, total, startAt, maxResults}``; mapped to
the shared ``SearchResult`` (``total_count`` and offset paging present).
"""

from __future__ import annotations

import re
from typing import Any

from cmcps.adapters.common import json_body
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "cern-jira"

BASE = "https://its.cern.ch/jira/rest/api/2"
BROWSE = "https://its.cern.ch/jira/browse"

_SEARCH_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,created,updated,labels,components,project"
)
_ISSUE_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,created,updated,"
    "description,labels,components,project,attachment,comment"
)
_MAX_RESULTS_LIMIT = 100
_NOT_FOUND = 404
_DESCRIPTION_LIMIT = 20000
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9]+$")


def _person(fields: Any) -> str | None:
    if not isinstance(fields, dict):
        return None
    return fields.get("displayName") or fields.get("name")


def _sub(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v.get("name")) for v in value if isinstance(v, dict) and v.get("name")]


def _issue_item(base: str, issue: dict[str, Any], *, detail: bool) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    project = fields.get("project") or {}
    status = fields.get("status") or {}
    item: dict[str, Any] = {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "url": f"{BROWSE}/{issue.get('key')}",
        "project": project.get("key") or project.get("name"),
        "status": status.get("name"),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "assignee": _person(fields.get("assignee")),
        "reporter": _person(fields.get("reporter")),
        "created_at": fields.get("created"),
        "updated_at": fields.get("updated"),
        "labels": fields.get("labels") or [],
        "components": [c.get("name") for c in fields.get("components") or []],
    }
    if detail:
        description = fields.get("description")
        if isinstance(description, str) and len(description) > _DESCRIPTION_LIMIT:
            description = description[:_DESCRIPTION_LIMIT] + "\n[truncated]"
        attachments = []
        for att in fields.get("attachment") or []:
            if not isinstance(att, dict):
                continue
            content = att.get("content")
            if isinstance(content, str) and content.startswith(f"{base}/jira/"):
                content = content.replace(f"{base}/jira/", "https://its.cern.ch/jira/", 1)
            attachments.append(
                {
                    "filename": att.get("filename"),
                    "size_bytes": att.get("size"),
                    "mime_type": att.get("mimeType"),
                    "url": content,
                }
            )
        comments = ((fields.get("comment") or {}).get("comments") or [])[:50]
        item.update(
            {
                "description": description,
                "attachments": attachments,
                "comment_count": (fields.get("comment") or {}).get("total", len(comments)),
                "comments": [
                    {
                        "author": _person(c.get("author")),
                        "created_at": c.get("created"),
                        "body": (c.get("body") or "")[:4000],
                    }
                    for c in comments
                    if isinstance(c, dict)
                ],
            }
        )
    return item


async def search_issues(
    client: SafeHttpClient,
    *,
    jql: str,
    start_at: int = 0,
    max_results: int = 50,
) -> SearchResult:
    """JQL search over CERN Jira issues."""
    if not jql.strip():
        raise InvalidInputError(
            "jql must not be empty",
            instructions="Pass a JQL query like project = CMSTRACK AND text ~ 'calibration'.",
        )
    if not 1 <= max_results <= _MAX_RESULTS_LIMIT:
        raise InvalidInputError(
            f"max_results must be between 1 and {_MAX_RESULTS_LIMIT}",
            instructions="Reduce max_results and page with start_at.",
        )
    if start_at < 0:
        raise InvalidInputError("start_at must be >= 0")
    response = await client.request(
        "GET",
        f"{BASE}/search",
        params={
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": _SEARCH_FIELDS,
        },
    )
    payload = json_body(response, source="cern-jira search")
    issues = payload.get("issues") or []
    items = [_issue_item(BASE, i, detail=False) for i in issues if isinstance(i, dict)]
    total = payload.get("total")
    return SearchResult(
        source=SOURCE,
        query=jql,
        items=items,
        total_count=total if isinstance(total, int) else None,
        page=(start_at // max_results) + 1,
        size=len(items),
        has_more=isinstance(total, int) and start_at + len(items) < total,
    )


async def get_issue(
    client: SafeHttpClient,
    *,
    issue_key: str,
) -> dict[str, Any]:
    """Full record of one Jira issue (KEY-123), with comments/attachments."""
    key = issue_key.strip().upper()
    if not _KEY_RE.fullmatch(key):
        raise InvalidInputError(
            f"invalid issue key {issue_key!r}",
            instructions="Expected format like CMSTRACK-123 from jira_search results.",
        )
    response = await client.request("GET", f"{BASE}/issue/{key}", params={"fields": _ISSUE_FIELDS})
    if response.status_code == _NOT_FOUND:
        raise UpstreamError(
            f"issue {key} not found or not visible",
            instructions="The issue may be restricted; sign in with cern_auth_sign_in.",
        )
    payload = json_body(response, source=f"cern-jira issue {key}")
    if not isinstance(payload, dict) or not payload.get("key"):
        raise UpstreamError(
            f"cern-jira returned no issue data for {key}",
            instructions="Check the key from jira_search results.",
        )
    return {
        "source": SOURCE,
        **_issue_item(BASE, payload, detail=True),
    }


async def get_project(
    client: SafeHttpClient,
    *,
    project_key: str,
) -> dict[str, Any]:
    """Metadata of one Jira project (key, lead, components, issue types)."""
    key = project_key.strip().upper()
    if not _PROJECT_RE.fullmatch(key):
        raise InvalidInputError(
            f"invalid project key {project_key!r}",
            instructions="Project keys are uppercase Jira codes like CMSTRACK.",
        )
    response = await client.request("GET", f"{BASE}/project/{key}")
    if response.status_code == _NOT_FOUND:
        raise UpstreamError(
            f"project {key} not found or not visible",
            instructions="Check the key; restricted projects need cern_auth_sign_in.",
        )
    payload = json_body(response, source=f"cern-jira project {key}")
    if not isinstance(payload, dict) or not payload.get("key"):
        raise UpstreamError(
            f"cern-jira returned no project data for {key}",
            instructions="Check the project key.",
        )
    return {
        "source": SOURCE,
        "key": payload.get("key"),
        "name": payload.get("name"),
        "description": payload.get("description"),
        "lead": _person(payload.get("lead")),
        "components": _sub(payload.get("components")),
        "issue_types": _sub(payload.get("issueTypes")),
        "url": f"{BROWSE}/{payload.get('key')}",
    }
