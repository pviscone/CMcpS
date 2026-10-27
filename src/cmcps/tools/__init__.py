"""MCP adapter tool registration for CMcpS."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from cmcps.auth.service import AuthService
from cmcps.config.settings import Settings
from cmcps.net.client import SafeHttpClient
from cmcps.tools.cds import register_cds_tools
from cmcps.tools.cmsonline import register_cmsonline_tools
from cmcps.tools.cmsresults import register_cmsresults_tools
from cmcps.tools.cmssdt import register_cmssdt_tools
from cmcps.tools.cmssearch import register_cmssearch_tools
from cmcps.tools.das import register_das_tools
from cmcps.tools.discourse import register_discourse_tools
from cmcps.tools.docdb import register_docdb_tools
from cmcps.tools.dqm import register_dqm_tools
from cmcps.tools.edms import register_edms_tools
from cmcps.tools.eguide import register_eguide_tools
from cmcps.tools.elog import register_elog_tools
from cmcps.tools.gitlab import register_gitlab_tools
from cmcps.tools.glance import register_glance_tools
from cmcps.tools.gocms import register_gocms_tools
from cmcps.tools.hltinfo import register_hltinfo_tools
from cmcps.tools.hypernews import register_hypernews_tools
from cmcps.tools.indico import register_indico_tools
from cmcps.tools.inspire import register_inspire_tools
from cmcps.tools.jira import register_jira_tools
from cmcps.tools.mattermost import register_mattermost_tools
from cmcps.tools.mcm import register_mcm_tools
from cmcps.tools.mkdocs import register_mkdocs_tools
from cmcps.tools.pdftools import register_pdftools_tools
from cmcps.tools.runregistry import register_runregistry_tools
from cmcps.tools.servicenow import register_servicenow_tools
from cmcps.tools.twiki import register_twiki_tools
from cmcps.tools.websites import register_websites_tools
from cmcps.tools.xsdb import register_xsdb_tools


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
    register_websites_tools(server, settings, auth, http)
    register_servicenow_tools(server, settings, auth, http)
    register_runregistry_tools(server, settings, auth, http)
