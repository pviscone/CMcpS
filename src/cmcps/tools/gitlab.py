"""MCP tool registration for the GitLab adapter."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmcps.adapters import gitlab
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.contracts.results import ToolEnvelope
from cmcps.net.client import SafeHttpClient
from cmcps.tools.common import guarded


def register_gitlab_tools(  # noqa: C901
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the read-only gitlab.cern.ch tools."""

    @server.tool(
        name="gitlab_search_projects",
        description=(
            "Search projects on gitlab.cern.ch by text, programming "
            "language, or topic — CERN GitLab repositories and the code inside "
            "them. Returns every project visible to the authenticated identity "
            "(public plus internal/private when signed in or a "
            "CMCPS_GITLAB_TOKEN is set); pass visibility to restrict. NOT for "
            "Jira issues (jira_search) or forum discussions "
            "(discourse_search). Projects may later be addressed by numeric id "
            "or full path (e.g. 'group/name')."
        ),
    )
    async def gitlab_search_projects(
        *,
        query: str | None = None,
        language: str | None = None,
        topic: str | None = None,
        visibility: str | None = None,
        sort_by: str = "last_activity_at",
        order: str = "desc",
        per_page: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """GitLab project search."""
        return await guarded(
            auth,
            lambda: gitlab.search_projects(
                http,
                settings,
                query=query,
                language=language,
                topic=topic,
                visibility=visibility,
                sort_by=sort_by,
                order=order,
                per_page=per_page,
                page=page,
            ),
        )

    @server.tool(
        name="gitlab_get_project",
        description=(
            "Detailed metadata of one gitlab.cern.ch project (id or "
            "'group/name'): statistics, languages, license, and namespace; "
            "follow gitlab_search_projects."
        ),
    )
    async def gitlab_get_project(project: str) -> ToolEnvelope:
        """GitLab project detail."""
        return await guarded(auth, lambda: gitlab.get_project(http, settings, project=project))

    @server.tool(
        name="gitlab_list_files",
        description=(
            "List files and directories of one gitlab.cern.ch project "
            "repository tree at an optional path and ref; follow "
            "gitlab_get_project."
        ),
    )
    async def gitlab_list_files(
        *,
        project: str,
        path: str | None = None,
        ref: str | None = None,
        recursive: bool = False,
        per_page: Annotated[int, Field(ge=1, le=100)] = 100,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """Repository tree listing."""
        return await guarded(
            auth,
            lambda: gitlab.list_files(
                http,
                settings,
                project=project,
                path=path,
                ref=ref,
                recursive=recursive,
                per_page=per_page,
                page=page,
            ),
        )

    @server.tool(
        name="gitlab_get_file",
        description=(
            "Read one text file from a gitlab.cern.ch repository (id or "
            "'group/name'); paths come from gitlab_list_files or "
            "gitlab_search_code. Binary files are reported without content."
        ),
    )
    async def gitlab_get_file(
        project: str,
        file_path: str,
        ref: str | None = None,
    ) -> ToolEnvelope:
        """Fetch and decode one repository file."""
        return await guarded(
            auth,
            lambda: gitlab.get_file(http, settings, project=project, file_path=file_path, ref=ref),
        )

    @server.tool(
        name="gitlab_get_readme",
        description=(
            "Locate and return the README of one gitlab.cern.ch project by "
            "probing common candidate file names; follow "
            "gitlab_search_projects."
        ),
    )
    async def gitlab_get_readme(
        project: str,
        ref: str | None = None,
    ) -> ToolEnvelope:
        """README lookup by candidate iteration."""
        return await guarded(
            auth, lambda: gitlab.get_readme(http, settings, project=project, ref=ref)
        )

    @server.tool(
        name="gitlab_search_code",
        description=(
            "Search code content (scope='blobs') or file names "
            "(scope='filenames') on gitlab.cern.ch, globally or within one "
            "project — 'where is symbol/config X used in CERN repos'. "
            "Requires advanced search to be enabled on the instance."
        ),
    )
    async def gitlab_search_code(
        *,
        search_term: str,
        project: str | None = None,
        scope: str = "blobs",
        ref: str | None = None,
        per_page: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """GitLab code search."""
        return await guarded(
            auth,
            lambda: gitlab.search_code(
                http,
                settings,
                search_term=search_term,
                project=project,
                scope=scope,
                ref=ref,
                per_page=per_page,
                page=page,
            ),
        )

    @server.tool(
        name="gitlab_search_issues",
        description=(
            "Search issues on gitlab.cern.ch, globally or within one project, "
            "filtered by state — repository-scoped issues; for CERN-wide "
            "service requests and project bugs use jira_search."
        ),
    )
    async def gitlab_search_issues(
        *,
        search_term: str,
        project: str | None = None,
        state: str = "all",
        per_page: Annotated[int, Field(ge=1, le=100)] = 10,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """GitLab issue search."""
        return await guarded(
            auth,
            lambda: gitlab.search_issues(
                http,
                settings,
                search_term=search_term,
                project=project,
                state=state,
                per_page=per_page,
                page=page,
            ),
        )

    @server.tool(
        name="gitlab_list_merge_requests",
        description=(
            "List merge requests of one gitlab.cern.ch project, filtered by "
            "state and ordered by creation or update time — 'latest open MRs "
            "of project X', 'recently merged MRs of X'. Requires access to the "
            "project (SSO cookies or CMCPS_GITLAB_TOKEN for "
            "private/internal)."
        ),
    )
    async def gitlab_list_merge_requests(
        *,
        project: str,
        state: str = "opened",
        order_by: str = "created_at",
        sort: str = "desc",
        search: str | None = None,
        per_page: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """Project merge request listing."""
        return await guarded(
            auth,
            lambda: gitlab.list_merge_requests(
                http,
                settings,
                project=project,
                state=state,
                order_by=order_by,
                sort=sort,
                search=search,
                per_page=per_page,
                page=page,
            ),
        )

    @server.tool(
        name="gitlab_get_wiki",
        description=(
            "List the wiki pages of one gitlab.cern.ch project, or fetch one "
            "page with content when page_slug is given; project ids/paths come "
            "from gitlab_search_projects."
        ),
    )
    async def gitlab_get_wiki(
        project: str,
        page_slug: str | None = None,
    ) -> ToolEnvelope:
        """Project wiki listing or single page."""
        return await guarded(
            auth,
            lambda: gitlab.get_wiki(http, settings, project=project, page_slug=page_slug),
        )

    @server.tool(
        name="gitlab_list_branches",
        description="List branches of a gitlab.cern.ch project repository.",
    )
    async def gitlab_list_branches(
        *,
        project: str,
        search: str | None = None,
        per_page: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """Repository branch listing."""
        return await guarded(
            auth,
            lambda: gitlab.list_branches(
                http, settings, project=project, search=search, per_page=per_page, page=page
            ),
        )

    @server.tool(
        name="gitlab_list_releases",
        description="List releases of a gitlab.cern.ch project.",
    )
    async def gitlab_list_releases(
        *,
        project: str,
        per_page: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """Project release listing."""
        return await guarded(
            auth,
            lambda: gitlab.list_releases(
                http, settings, project=project, per_page=per_page, page=page
            ),
        )

    @server.tool(
        name="gitlab_get_release",
        description="Fetch one release of a gitlab.cern.ch project by tag name.",
    )
    async def gitlab_get_release(project: str, tag_name: str) -> ToolEnvelope:
        """Single release detail."""
        return await guarded(
            auth,
            lambda: gitlab.get_release(http, settings, project=project, tag_name=tag_name),
        )

    @server.tool(
        name="gitlab_list_tags",
        description="List tags of a gitlab.cern.ch project repository.",
    )
    async def gitlab_list_tags(
        *,
        project: str,
        search: str | None = None,
        sort: str = "desc",
        per_page: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> ToolEnvelope:
        """Repository tag listing."""
        return await guarded(
            auth,
            lambda: gitlab.list_tags(
                http,
                settings,
                project=project,
                search=search,
                sort=sort,
                per_page=per_page,
                page=page,
            ),
        )
