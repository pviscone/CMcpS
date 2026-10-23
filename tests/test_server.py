"""MCP tool registration and auth-guard integration tests."""

from __future__ import annotations

from typing import Any

import pytest

from cmcps.app import build_server
from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings

BOOTSTRAP_TOOLS = {
    "cern_auth_sign_in",
    "cern_auth_reauthenticate",
    "cern_auth_status",
    "cern_health",
}
INDICO_TOOLS = {
    "indico_get_user_info",
    "indico_search_events",
    "indico_search",
    "indico_get_event",
    "indico_get_event_contributions",
    "indico_get_event_files",
    "indico_download_file",
}
GITLAB_TOOLS = {
    "gitlab_search_projects",
    "gitlab_get_project",
    "gitlab_list_files",
    "gitlab_get_file",
    "gitlab_get_readme",
    "gitlab_search_code",
    "gitlab_search_issues",
    "gitlab_get_wiki",
    "gitlab_list_branches",
    "gitlab_list_releases",
    "gitlab_get_release",
    "gitlab_list_tags",
}
CDS_TOOLS = {"cds_search", "cds_get_record", "cds_get_files", "cds_download_file"}
INSPIRE_TOOLS = {
    "inspire_search_papers",
    "inspire_get_paper",
    "inspire_get_author_papers",
    "inspire_get_citations",
    "inspire_search_collaboration",
    "inspire_get_paper_figures",
    "inspire_get_references",
    "inspire_get_bibtex",
}
DISCOURSE_TOOLS = {
    "discourse_search",
    "discourse_list_topics",
    "discourse_list_categories",
    "discourse_get_topic",
    "discourse_get_post",
    "discourse_get_user",
    "discourse_download_file",
}
ELOG_TOOLS = {
    "cmsonline_elog_search",
    "cmsonline_elog_get_detail",
    "cmsonline_elog_list_logbooks",
    "cmsonline_elog_download_attachment",
}
MKDOCS_TOOLS = {
    "mkdocs_search",
    "mkdocs_list_pages",
    "mkdocs_get_page",
    "mkdocs_download_file",
}
HLTINFO_TOOLS = {
    "hltinfo_list_menus",
    "hltinfo_get_path_summary",
    "hltinfo_get_path_names",
    "hltinfo_get_path",
    "hltinfo_get_path_prescales",
    "hltinfo_get_run_lumis",
}
CMSSDT_TOOLS = {
    "cmssdt_list_releases",
    "cmssdt_list_ib_queues",
    "cmssdt_ib_summary",
    "cmssdt_get_ib_details",
    "cmssdt_list_cmssw_labels",
    "cmssdt_list_open_prs",
    "cmssdt_relval_archs",
    "cmssdt_build_log_url",
}
CMSSEARCH_TOOLS = {
    "cmssearch_search",
    "cmssearch_get_document",
    "cmssearch_list_sources",
    "cmssearch_index_stats",
}
GLANCE_TOOLS = {
    "glance_alcm_user",
    "glance_membership_user",
    "glance_search_analyses",
    "glance_list_published_analyses",
    "glance_get_analysis",
    "glance_list_note_types",
    "glance_search_notes",
    "glance_get_note",
    "glance_list_projects",
    "glance_list_alcm_working_groups",
    "glance_alcm_members",
    "glance_membership_search_members",
}
XSDB_TOOLS = {
    "xsdb_search",
    "xsdb_list_tables",
}
GOCMS_TOOLS = {
    "gocms_list_links",
    "gocms_get_link",
    "gocms_list_tags",
}
DAS_TOOLS = {
    "das_search",
    "das_get_dataset_info",
}
MCM_TOOLS = {
    "mcm_get_request",
    "mcm_get_status_and_approval",
    "mcm_get_output_datasets",
    "mcm_find_producing_requests",
}
JIRA_TOOLS = {
    "jira_search",
    "jira_get_issue",
    "jira_get_project",
}
CMSONLINE_TOOLS = {
    "cmsonline_get_version",
    "cmsonline_list_runs",
    "cmsonline_get_run",
    "cmsonline_list_fills",
    "cmsonline_list_lumisections",
    "cmsonline_get_shift_schedule",
}
TWIKI_TOOLS = {
    "twiki_search",
    "twiki_get_page",
    "twiki_list_topics",
    "twiki_download_attachment",
}
CMSRESULTS_TOOLS = {
    "cmsresults_list_groups",
    "cmsresults_list_papers",
    "cmsresults_get_paper",
    "cmsresults_download_file",
}
ALL_TOOLS = (
    BOOTSTRAP_TOOLS
    | INDICO_TOOLS
    | GITLAB_TOOLS
    | CDS_TOOLS
    | INSPIRE_TOOLS
    | DISCOURSE_TOOLS
    | CMSRESULTS_TOOLS
    | TWIKI_TOOLS
    | CMSONLINE_TOOLS
    | JIRA_TOOLS
    | MCM_TOOLS
    | DAS_TOOLS
    | GOCMS_TOOLS
    | CMSSDT_TOOLS
    | XSDB_TOOLS
    | HLTINFO_TOOLS
    | MKDOCS_TOOLS
    | ELOG_TOOLS
    | CMSSEARCH_TOOLS
    | GLANCE_TOOLS
)

# Minimal arguments per adapter tool, used to prove the auth guard fires before
# any argument-specific backend work.
ADAPTER_CALLS: dict[str, dict[str, Any]] = {
    "indico_get_user_info": {},
    "indico_search_events": {},
    "indico_search": {"query": "higgs"},
    "indico_get_event": {"event_id": "1"},
    "indico_get_event_contributions": {"event_id": "1"},
    "indico_get_event_files": {"event_id": "1"},
    "indico_download_file": {"download_url": "https://indico.cern.ch/event/1/material/1/1.bin"},
    "gitlab_search_projects": {},
    "gitlab_get_project": {"project": "123"},
    "gitlab_list_files": {"project": "123"},
    "gitlab_get_file": {"project": "123", "file_path": "README.md"},
    "gitlab_get_readme": {"project": "123"},
    "gitlab_search_code": {"search_term": "jet"},
    "gitlab_search_issues": {"search_term": "leak"},
    "gitlab_get_wiki": {"project": "123"},
    "gitlab_list_branches": {"project": "123"},
    "gitlab_list_releases": {"project": "123"},
    "gitlab_get_release": {"project": "123", "tag_name": "v1"},
    "gitlab_list_tags": {"project": "123"},
    "cds_search": {"query": "higgs"},
    "cds_get_record": {"recid": "123"},
    "cds_get_files": {"recid": "123"},
    "cds_download_file": {"download_url": "https://cds.cern.ch/record/1/files/a.pdf"},
    "inspire_search_papers": {"query": "higgs"},
    "inspire_get_paper": {"inspire_id": "123"},
    "inspire_get_author_papers": {"author_name": "Weinberg, Steven"},
    "inspire_get_citations": {"inspire_id": "123"},
    "inspire_search_collaboration": {"collaboration_name": "CMS"},
    "inspire_get_paper_figures": {"inspire_id": "123"},
    "inspire_get_references": {"inspire_id": "123"},
    "inspire_get_bibtex": {"inspire_id": "123"},
    "discourse_search": {"site": "root-forum", "query": "TTree"},
    "discourse_list_topics": {"site": "root-forum"},
    "discourse_list_categories": {"site": "root-forum"},
    "discourse_get_topic": {"site": "root-forum", "topic_id": 1},
    "discourse_get_post": {"site": "root-forum", "post_id": 1},
    "discourse_get_user": {"site": "root-forum", "username": "Axel"},
    "discourse_download_file": {
        "site": "root-forum",
        "download_url": "https://root-forum.cern.ch/uploads/default/1/a.bin",
    },
    "cmsresults_list_groups": {},
    "cmsresults_list_papers": {"group": "HIG"},
    "cmsresults_get_paper": {"paper_id": "HIG-11-024"},
    "cmsresults_download_file": {
        "download_url": "https://cms-results.web.cern.ch/cms-results/public-results/publications/HIG-11-024/figures/a.png"
    },
    "twiki_search": {"web": "CMSPublic", "query": "higgs"},
    "twiki_get_page": {"web": "CMSPublic", "topic": "WebHome"},
    "twiki_list_topics": {"web": "CMSPublic"},
    "twiki_download_attachment": {
        "attachment_url": "https://twiki.cern.ch/twiki/pub/CMSPublic/WebHome/a.pdf"
    },
    "cmsonline_get_version": {},
    "cmsonline_list_runs": {},
    "cmsonline_get_run": {"run_number": 1},
    "cmsonline_list_fills": {},
    "cmsonline_list_lumisections": {"run_number": 1},
    "jira_search": {"jql": "project = CMSTRACK"},
    "jira_get_issue": {"issue_key": "CMSTRACK-1"},
    "jira_get_project": {"project_key": "CMSTRACK"},
    "mcm_get_request": {"request_id": "B2G-RunIISummer20UL17wmLHEGEN-00001"},
    "mcm_get_status_and_approval": {"prepid": "B2G-RunIISummer20UL17wmLHEGEN-00001"},
    "mcm_get_output_datasets": {"prepid": "B2G-RunIISummer20UL17wmLHEGEN-00001"},
    "mcm_find_producing_requests": {"dataset": "/TTbar/SIM-v1/AODSIM"},
    "das_search": {"query": "dataset=/TT*/RunII*/AODSIM"},
    "das_get_dataset_info": {"dataset": "/TTbar/SIM-v1/AODSIM"},
    "gocms_list_links": {},
    "gocms_get_link": {"link_id": 1},
    "gocms_list_tags": {},
    "cmssdt_list_releases": {},
    "cmssdt_list_ib_queues": {},
    "cmssdt_ib_summary": {},
    "cmssdt_get_ib_details": {"queue": "CMSSW_20_1_X"},
    "cmssdt_list_cmssw_labels": {},
    "cmssdt_list_open_prs": {"family": "CMSSW_20_1"},
    "cmssdt_relval_archs": {},
    "cmssdt_build_log_url": {
        "ib_name": "CMSSW_20_1_X_2026-10-22-1100",
        "arch": "el9_amd64_gcc14",
    },
    "cmssearch_search": {"query": "calibration"},
    "cmssearch_get_document": {"source_key": "cms-jme-public", "document_id": "c5b999"},
    "cmssearch_list_sources": {},
    "cmssearch_index_stats": {},
    "glance_alcm_user": {},
    "glance_membership_user": {},
    "glance_search_analyses": {},
    "glance_get_analysis": {"analysis": "EXO-24-010"},
    "glance_list_published_analyses": {},
    "glance_list_note_types": {},
    "glance_search_notes": {},
    "glance_get_note": {"note_id": 1},
    "glance_list_projects": {},
    "glance_list_alcm_working_groups": {},
    "glance_alcm_members": {},
    "glance_membership_search_members": {"query": "piero"},
    "xsdb_search": {"particles": "ttbar"},
    "xsdb_list_tables": {},
    "hltinfo_list_menus": {},
    "hltinfo_get_path_summary": {},
    "hltinfo_get_path_names": {},
    "hltinfo_get_path": {"path_name": "HLT_Ele32_WPTight_Gsf"},
    "hltinfo_get_path_prescales": {"path_name": "HLT_Ele32_WPTight_Gsf"},
    "hltinfo_get_run_lumis": {"path_name": "HLT_Ele32_WPTight_Gsf"},
    "mkdocs_search": {"base_url": "https://sftroot.docs.cern.ch", "query": "trigger"},
    "mkdocs_list_pages": {"base_url": "https://sftroot.docs.cern.ch"},
    "mkdocs_get_page": {"base_url": "https://sftroot.docs.cern.ch", "page": ""},
    "mkdocs_download_file": {"file_url": "https://sftroot.docs.cern.ch/a.png"},
    "cmsonline_elog_search": {"query": "beam"},
    "cmsonline_elog_get_detail": {"msg_id": 1347902},
    "cmsonline_elog_list_logbooks": {},
    "cmsonline_elog_download_attachment": {
        "attachment_url": "https://cmsonline.cern.ch/Elog/htdocs/data/495/a.png"
    },
}


async def test_server_exposes_m1_tools(settings: Settings) -> None:
    server = build_server(settings)
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert names == ALL_TOOLS
    sign_in = next(tool for tool in tools if tool.name == "cern_auth_sign_in")
    timeout_schema = sign_in.input_schema["properties"]["timeout_seconds"]
    bounded = timeout_schema["anyOf"][0]
    assert bounded["minimum"] == 30
    assert bounded["maximum"] == 900


async def test_protected_health_returns_structured_auth_required(
    settings: Settings,
) -> None:
    server = build_server(settings)
    result = await server.call_tool("cern_health", {})
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    assert "cern_auth_sign_in" in payload["error"]["instructions"]


@pytest.mark.parametrize("tool_name", sorted(ADAPTER_CALLS))
async def test_adapter_tools_are_auth_gated(settings: Settings, tool_name: str) -> None:
    server = build_server(settings)
    result = await server.call_tool(tool_name, ADAPTER_CALLS[tool_name])
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    assert "cern_auth_sign_in" in payload["error"]["instructions"]


async def test_explicit_bypass_makes_health_ok(tmp_path) -> None:
    settings = Settings(state_dir=tmp_path, require_sso=False)
    server = build_server(settings)
    result = await server.call_tool("cern_health", {})
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "ok"
    assert payload["data"]["authenticated"] is True
    assert payload["data"]["auth_bypass"] is True
    assert payload["data"]["read_only_methods"] == ["GET", "HEAD"]


async def test_status_and_browser_sign_in_return_secret_free_metadata(
    settings: Settings,
    valid_storage_state: dict[str, Any],
) -> None:
    async def collector(_: Settings, timeout: int | None) -> dict[str, Any]:
        assert timeout == 60
        return valid_storage_state

    auth = AuthService(settings, collector=collector)
    server = build_server(settings, auth_service=auth)

    status_before = await server.call_tool("cern_auth_status", {})
    assert status_before.structured_content["status"] == "ok"
    assert status_before.structured_content["data"]["authenticated"] is False

    signed_in = await server.call_tool("cern_auth_sign_in", {"timeout_seconds": 60})
    assert signed_in.structured_content["status"] == "ok"
    assert signed_in.structured_content["data"]["state_file"] == settings.session_path.name
    assert "state_path" not in signed_in.structured_content["data"]
    serialized = signed_in.content[0].text
    assert "sso-secret-value" not in serialized
    assert "service-secret-value" not in serialized

    health = await server.call_tool("cern_health", {})
    assert health.structured_content["status"] == "ok"
    assert health.structured_content["data"]["authenticated"] is True
