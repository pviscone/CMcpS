"""User-confirmed write actions on CERN web services.

Each function receives an :class:`~cmsmcp.policy.ApprovedAction` from the
policy engine (the user confirmed this exact payload) and performs one write
through :meth:`SafeHttpClient.request_action`:

- Mattermost: post a message or reply (session cookie or personal token).
- GitLab: comment on an issue / merge request, open an issue (needs a GitLab
  token with the ``api`` scope; the API refuses session-cookie writes).
- Jira: comment on an issue (needs a Jira personal access token).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, UpstreamError, setup_hint
from cmsmcp.net.client import SafeHttpClient, SafeResponse
from cmsmcp.policy import ApprovedAction

MATTERMOST_HOST = "mattermost.web.cern.ch"
GITLAB_HOST = "gitlab.cern.ch"
JIRA_HOST = "its.cern.ch"
_ID_RE = re.compile(r"^[a-z0-9]{26}$")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_CREATED = frozenset({200, 201})
# GitLab executes quick actions (/merge, /approve, /close, /assign, ...) found in
# notes and descriptions created through the API: a confirmed "comment" could
# carry a state change the confirmation never named.
_QUICK_ACTION_RE = re.compile(r"^\s*/[a-z_]+", re.MULTILINE)


def check_no_quick_actions(text: str, what: str) -> str:
    if _QUICK_ACTION_RE.search(text):
        raise InvalidInputError(
            f"the {what} contains a line starting with '/', which GitLab would run as a "
            "quick action (e.g. /merge, /approve, /close)",
            instructions="Indent or rephrase that line; quick actions are not supported.",
        )
    return text


def _check(response: SafeResponse, what: str) -> dict[str, Any]:
    if response.status_code not in _CREATED:
        body = response.content.decode(errors="replace")[:300]
        raise UpstreamError(
            f"{what} failed with HTTP {response.status_code}: {body}",
            status_code=response.status_code,
        )
    try:
        data = json.loads(response.content)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------- Mattermost
def check_mattermost_id(value: str, what: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise InvalidInputError(f"bad Mattermost {what} id {value!r}")
    return value


async def mattermost_post(
    client: SafeHttpClient,
    settings: Settings,
    approval: ApprovedAction,
    *,
    channel_id: str,
    message: str,
    root_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"channel_id": channel_id, "message": message}
    if root_id:
        body["root_id"] = root_id
    headers = {"X-Requested-With": "XMLHttpRequest"}
    token = settings.mattermost_token
    response = await client.request_action(
        approval,
        "POST",
        f"{settings.mattermost_base_url.rstrip('/')}/api/v4/posts",
        json_body=body,
        headers=headers,
        service_headers={"Authorization": f"Bearer {token}"} if token else None,
    )
    post = _check(response, "Mattermost post")
    post_id = post.get("id")
    return {
        "posted": True,
        "post_id": post_id,
        "url": f"{settings.mattermost_base_url.rstrip('/')}/_redirect/pl/{post_id}"
        if post_id
        else None,
    }


# --------------------------------------------------------------------- GitLab
def _gitlab_token(settings: Settings) -> str:
    if not settings.gitlab_token:
        raise InvalidInputError(
            "GitLab writes need a personal access token with the api scope",
            instructions=setup_hint("gitlab", "A GitLab token with the api scope"),
        )
    return settings.gitlab_token


def _project(project: str) -> str:
    text = str(project).strip()
    if not text:
        raise InvalidInputError("project must not be empty")
    return text if text.isdigit() else quote(text, safe="")


async def gitlab_comment(
    client: SafeHttpClient,
    settings: Settings,
    approval: ApprovedAction,
    *,
    project: str,
    kind: str,
    iid: int,
    body: str,
) -> dict[str, Any]:
    token = _gitlab_token(settings)
    collection = {"issue": "issues", "merge_request": "merge_requests"}.get(kind)
    if collection is None:
        raise InvalidInputError("kind must be 'issue' or 'merge_request'")
    url = (
        f"{settings.gitlab_base_url.rstrip('/')}/api/v4/projects/{_project(project)}"
        f"/{collection}/{iid}/notes"
    )
    response = await client.request_action(
        approval,
        "POST",
        url,
        json_body={"body": body},
        authenticated=False,
        service_headers={"PRIVATE-TOKEN": token},
    )
    note = _check(response, "GitLab comment")
    return {"commented": True, "note_id": note.get("id"), "project": project, "iid": iid}


async def gitlab_create_issue(
    client: SafeHttpClient,
    settings: Settings,
    approval: ApprovedAction,
    *,
    project: str,
    title: str,
    description: str,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    token = _gitlab_token(settings)
    payload: dict[str, Any] = {"title": title, "description": description}
    if labels:
        payload["labels"] = ",".join(labels)
    response = await client.request_action(
        approval,
        "POST",
        f"{settings.gitlab_base_url.rstrip('/')}/api/v4/projects/{_project(project)}/issues",
        json_body=payload,
        authenticated=False,
        service_headers={"PRIVATE-TOKEN": token},
    )
    issue = _check(response, "GitLab issue")
    return {"created": True, "iid": issue.get("iid"), "url": issue.get("web_url")}


# ----------------------------------------------------------------------- Jira
def check_jira_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise InvalidInputError(f"bad Jira issue key {key!r}", instructions="e.g. CMSTRACK-123")
    return key


async def jira_comment(
    client: SafeHttpClient,
    settings: Settings,
    approval: ApprovedAction,
    *,
    key: str,
    body: str,
) -> dict[str, Any]:
    if not settings.jira_token:
        raise InvalidInputError(
            "Jira writes need a personal access token",
            instructions=setup_hint("jira", "A Jira personal access token"),
        )
    response = await client.request_action(
        approval,
        "POST",
        f"https://{JIRA_HOST}/jira/rest/api/2/issue/{key}/comment",
        json_body={"body": body},
        authenticated=False,
        service_headers={"Authorization": f"Bearer {settings.jira_token}"},
    )
    comment = _check(response, "Jira comment")
    return {
        "commented": True,
        "comment_id": comment.get("id"),
        "url": f"https://{JIRA_HOST}/jira/browse/{key}",
    }
