"""MCP adapter tool registration for cmsmcp."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.tools.cds import register_cds_tools
from cmsmcp.tools.cmsonline import register_cmsonline_tools
from cmsmcp.tools.cmsresults import register_cmsresults_tools
from cmsmcp.tools.cmssdt import register_cmssdt_tools
from cmsmcp.tools.cmssearch import register_cmssearch_tools
from cmsmcp.tools.das import register_das_tools
from cmsmcp.tools.discourse import register_discourse_tools
from cmsmcp.tools.docdb import register_docdb_tools
from cmsmcp.tools.dqm import register_dqm_tools
from cmsmcp.tools.edms import register_edms_tools
from cmsmcp.tools.eguide import register_eguide_tools
from cmsmcp.tools.elog import register_elog_tools
from cmsmcp.tools.gitlab import register_gitlab_tools
from cmsmcp.tools.glance import register_glance_tools
from cmsmcp.tools.gocms import register_gocms_tools
from cmsmcp.tools.hltinfo import register_hltinfo_tools
from cmsmcp.tools.hypernews import register_hypernews_tools
from cmsmcp.tools.indico import register_indico_tools
from cmsmcp.tools.inspire import register_inspire_tools
from cmsmcp.tools.jira import register_jira_tools
from cmsmcp.tools.mattermost import register_mattermost_tools
from cmsmcp.tools.mcm import register_mcm_tools
from cmsmcp.tools.mkdocs import register_mkdocs_tools
from cmsmcp.tools.pdftools import register_pdftools_tools
from cmsmcp.tools.phonebook import register_phonebook_tools
from cmsmcp.tools.runregistry import register_runregistry_tools
from cmsmcp.tools.servicenow import register_servicenow_tools
from cmsmcp.tools.twiki import register_twiki_tools
from cmsmcp.tools.websites import register_websites_tools
from cmsmcp.tools.xsdb import register_xsdb_tools


def register_adapter_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register every M2 adapter tool set behind the shared guard."""
    register_indico_tools(server, settings, auth, http)
    register_inspire_tools(server, settings, auth, http)
    register_gitlab_tools(server, settings, auth, http)
    register_cds_tools(server, settings, auth, http)
    register_discourse_tools(server, settings, auth, http)
    register_cmsresults_tools(server, settings, auth, http)
    register_cmsonline_tools(server, settings, auth, http)
    register_jira_tools(server, settings, auth, http)
    register_mattermost_tools(server, settings, auth, http)
    register_mcm_tools(server, settings, http)
    register_das_tools(server, settings)
    register_dqm_tools(server, settings, http)
    register_gocms_tools(server, settings, auth, http)
    register_cmssdt_tools(server, settings, auth, http)
    register_xsdb_tools(server, settings, auth, http)
    register_hltinfo_tools(server, settings, auth, http)
    register_mkdocs_tools(server, settings, auth, http)
    register_elog_tools(server, settings, auth, http)
    register_twiki_tools(server, settings, auth, http)
    register_cmssearch_tools(server, settings, auth, http)
    register_glance_tools(server, settings, auth, http)
    register_docdb_tools(server, settings, auth, http)
    register_edms_tools(server, settings, auth, http)
    register_eguide_tools(server, settings, auth, http)
    register_hypernews_tools(server, settings, auth, http)
    register_pdftools_tools(server, settings, auth, http)
    register_phonebook_tools(server, settings, auth, http)
    register_websites_tools(server, settings, auth, http)
    register_servicenow_tools(server, settings, auth, http)
    register_runregistry_tools(server, settings, auth, http)
