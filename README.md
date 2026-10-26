# CMcpS

CMcpS is a read-only Model Context Protocol server for CERN and CMS resources. It consolidates the Indico, INSPIRE-HEP, CERN GitLab, and CDS MCP servers behind one authenticated foundation: a browser-backed CERN SSO session that must be established before protected tools run.

## Requirements

- Python 3.12 or newer
- Chromium, Chrome, or another Playwright-compatible browser for interactive SSO
- [uv](https://docs.astral.sh/uv/) for local development

## Setup and run

```bash
uv sync --group dev
uv run cmcps
```

For full functionality two environment variables are needed alongside the SSO session (`cern_auth_sign_in` covers everything else):

- `CMCPS_INDICO_API_TOKEN` — an Indico personal API token (`indp_...`, from indico.cern.ch → your profile → API). Without it, protected Indico events/categories are invisible (they surface as "not found").
- `CMCPS_GITLAB_TOKEN` — a GitLab personal access token with the `read_api` scope. Without it, internal/private GitLab projects, wikis, issues, and code search are inaccessible.
- DAS tools require `dasgoclient`

```bash
CMCPS_INDICO_API_TOKEN=indp_... CMCPS_GITLAB_TOKEN=glpat-... uv run cmcps
```

The server communicates over MCP stdio. Install it as an MCP client process or run the console entry point from the command above. `python -m cmcps` is equivalent.

### Run without a developer checkout (`uvx`)

`uvx`/`uv tool` runs the server in its own environment without `uv sync`:

```bash
# from a checkout
uvx --from /path/to/CMcpS cmcps

# or install once, then run anywhere
uv tool install --force /path/to/CMcpS
cmcps
```

MCP client configuration example:

```json
{
  "mcpServers": {
    "cmcps": {
      "command": "uvx",
      "args": ["--from", "/path/to/CMcpS", "cmcps"]
    }
  }
}
```

Plain `uvx cmcps` (no `--from`) resolves the name from PyPI and only works once the package is published there. When using git hosting instead, `uvx --from git+https://<host>/<org>/CMcpS.git cmcps` works the same way. The first run needs a Playwright-compatible system browser (Chromium/Chrome/Firefox) for `cern_auth_sign_in`; the server auto-detects installed browsers in the uvx environment.

Note: `uvx --from <local dir>` caches the built wheel per package *version*; source changes without a version bump keep serving the old build until you run `uv cache clean cmcps` (or bump the version).

## Auth and bootstrap tools

- `cern_auth_sign_in` — opens a visible browser at the CERN account page, waits for an authenticated Keycloak identity cookie, and stores only allowlisted cookies locally. No localStorage, cookie, or token values are returned to the MCP client.
- `cern_auth_status` — reports whether a usable session is stored, its expiry when known, and safe metadata such as the credential filename. Call this first when troubleshooting authentication.
- `cern_auth_reauthenticate` — discards the stored session cookies and captured service Bearer tokens, then runs a fresh visible-browser sign-in. Use when `cern_auth_sign_in` keeps reusing a broken session.
- `cern_health` — protected smoke tool proving the authentication guard, read-only safety policy, and server wiring after sign-in.

MCP does not guarantee client-side call order, so CMcpS enforces authentication server-side. Every adapter tool returns a structured `AUTH_REQUIRED` result until `cern_auth_sign_in` completes.

## Indico tools (indico.cern.ch)

`CMCPS_INDICO_API_TOKEN` (Indico personal API token, `indp_...`, created at indico.cern.ch → your profile → API) is optional for public data, **required for protected events and categories**, and for `indico_get_user_info`. The legacy export API ignores SSO cookies: without the token, protected content looks like "not found" (`200` with `count: 0`), which the adapter reports with setup instructions. When the token is set it is the only credential sent to Indico — the server answers HTTP 400 to requests carrying both cookies and a Bearer token.

- `indico_get_user_info` — profile of the configured API-token user.
- `indico_search_events` — events in a category (legacy export API, `category_id`, date range, `limit`/`offset`).
- `indico_search` — full-text search across Indico content types.
- `indico_get_event` — one event, optionally with contributions and sessions.
- `indico_get_event_contributions` — talks of an event, optionally subcontributions.
- `indico_get_event_files` — attachment metadata and download URLs for an event and its contributions.
- `indico_download_file` — bounded download of one attachment URL into local CMcpS storage; returns the local path and size, never inline content.

## INSPIRE-HEP tools (inspirehep.net/api)

Public API; no service token needed.

- `inspire_search_papers` — literature search with `q` query syntax, sort, size, page.
- `inspire_get_paper` — record detail by INSPIRE id, arXiv id, or DOI.
- `inspire_get_author_papers` — author publications and citation metrics (name or BAI).
- `inspire_get_citations` — citing or cited-by graph with a per-year timeline.
- `inspire_search_collaboration` — publications of an experiment with statistics.
- `inspire_get_paper_figures` — figure captions and URLs (links only).
- `inspire_get_references` — formatted references (BibTeX, LaTeX-US, LaTeX-EU, or JSON).
- `inspire_get_bibtex` — one BibTeX entry by any supported identifier.

## CERN GitLab tools (gitlab.cern.ch)

GitLab API access does **not** use the CERN SSO browser session: two independent reasons.

- The SSO sign-in flow never visits `gitlab.cern.ch`, so the stored session state never contains `_gitlab_session` (or any GitLab-scoped cookie). The persisted session holds cookies only for `auth.cern.ch` and the SSO-warmed sites (Indico, its.cern.ch, the CMS forums, the HLT-info and CMSFence apps, ...).
- Even with a session cookie, the GitLab API ignores browser cookies for most endpoints; requests stay anonymous and private projects answer `404`.

Facts about `CMCPS_GITLAB_TOKEN`:

- Create the token at `gitlab.cern.ch` → Preferences → Personal access tokens (direct URL: `https://gitlab.cern.ch/-/user_settings/personal_access_tokens`) with the **`read_api`** scope.
- Set it as the `CMCPS_GITLAB_TOKEN` environment variable; it is sent as the `PRIVATE-TOKEN` header and injected only by the trusted HTTP layer — never accepted as a tool argument, never logged.
- Without it: public project search, repository trees, public merge-request lists, and public file contents work anonymously. Private projects answer `404` and never appear in search results.
- Required for: `gitlab_search_issues`, `gitlab_get_wiki`, anything on private/internal projects (including `gitlab_list_merge_requests`), and code search (`gitlab_search_code` needs a token on CERN GitLab and advanced search for blobs).
- `gitlab_search_projects` returns every project visible to the authenticated identity (public ones anonymously, internal/private ones with the token); pass `visibility` to restrict the listing.
- `cern_auth_sign_in` is still required first (the SSO gate applies to every tool), but SSO alone never unlocks GitLab data — the token is the credential that matters.

- `gitlab_search_projects` — project search with language/topic/visibility filters; authenticated identities also see internal/private projects they can access.
- `gitlab_get_project` — project info including statistics and language breakdown.
- `gitlab_list_merge_requests` — merge requests of one project, state/order filters.
- `gitlab_list_files` — repository tree listing.
- `gitlab_get_file` — file content (text decoded; binaries return a placeholder).
- `gitlab_get_readme` — first README variant of a project.
- `gitlab_search_code` — blob or filename search, project-scoped or global.
- `gitlab_search_issues` — issue search, project-scoped or global.
- `gitlab_get_wiki` — wiki page list or page detail.
- `gitlab_list_branches` — branches with head commit info.
- `gitlab_list_releases` — release list.
- `gitlab_get_release` — one release with assets and evidence.
- `gitlab_list_tags` — tags with target commit info.

## CDS tools (cds.cern.ch)

Legacy Invenio search API. CDS occasionally serves an Anubis bot challenge to non-browser clients; a completed `cern_auth_sign_in` session usually passes it.

- `cds_search` — document search with experiment / document-type / date filters.
- `cds_get_record` — full record by recid (`123456` or `cds:123456`).
- `cds_get_files` — attachment metadata and download links for a record (links only).
- `cds_download_file` — bounded download of one attachment URL into local CMcpS storage; returns the local path and size. Restricted records and attachments (e.g. CWR paper drafts) require the cds.cern.ch SSO cookies captured by `cern_auth_sign_in`; a missing/expired session is detected (iCMS login placeholder / login HTML) and reported instead of returning HTML as a file.

All search/list tools share the `SearchResult` envelope (`source, items, total_count, page, size, has_more, query, sort, note`). The mapping of every original reference-server tool to its CMcpS tool (including intentional removals) is documented in [docs/m2-migration.md](docs/m2-migration.md).

## CERN Discourse forum tools (4 sites, one adapter)

Stock Discourse JSON API. `cms-talk` (CMS internal discussion) and `cms-pub-talk` (internal CMS publication-review discussion, one category per paper/publication) require CERN SSO; `root-forum` and `geant4-forum` are anonymously readable. Every tool takes a `site` argument: `cms-talk`, `cms-pub-talk`, `root-forum`, or `geant4-forum`. Endpoint discovery notes: [docs/m3-adapters.md](docs/m3-adapters.md).

- `discourse_search` — full-text search across posts and topics (1-based `page`).
- `discourse_list_topics` — latest/top (with `period`) or one category's topics (`category_id`).
- `discourse_list_categories` — category tree with topic/post counts.
- `discourse_get_topic` — topic detail plus one page of posts as plain text (HTML stripped, ~20 posts per page, attachments listed per post).
- `discourse_get_post` — single post with plain text and raw markdown when public.
- `discourse_get_user` — public user profile summary.
- `discourse_download_file` — bounded attachment/image download into local CMcpS storage (forum-host URLs only).

## CMS public results tools (cms-results.web.cern.ch)

Static HTML pages of CMS publications and preliminary results (public; no auth). The `cms-results-search` web app backend is **not** used — it was persistently unavailable during adapter discovery, so this adapter parses the machine-generated index and paper pages directly (see [docs/m3-adapters.md](docs/m3-adapters.md)).

- `cmsresults_list_groups` — physics working groups (HIG, SMP, TOP, EXO, ...) with publications/preliminary index links.
- `cmsresults_list_papers` — papers of one group (title, journal, DOI, date) with client-side `year`/`title_contains` filters and `kind` (`publications`/`preliminary-results`).
- `cmsresults_get_paper` — detail of one paper (e.g. `HIG-12-028`): abstract, journal/DOI, arXiv/CDS/INSPIRE links, figure/table URLs.
- `cmsresults_download_file` — bounded download of one figure/table/PDF hosted on cms-results.web.cern.ch.

## TWiki tools (twiki.cern.ch)

CERN TWiki serves rendered `view` pages anonymously for every web (restricted ones included). It no longer serves the wiki source — `?raw=on` returns the rendered page — so `twiki_get_page` fetches the low-noise print cover (`cover=print`) and returns plain text; `raw=true` additionally parses the footer metadata (revision, author, date, parent topic, attachments). Sign-in visits the TWiki `viewauth` script so the TWiki session cookie is available for scripts that gate on login (protected attachment downloads). **TWiki full-text search is done with `cmssearch_search`** (indices `cms-twiki` for the private web and `cms-public-twiki` for the public web) — the TWiki CGI search is slow/flaky and returns no snippets, so there is no `twiki_search` tool. Webs are hierarchical: subwebs are addressed as `Web/Subweb` (e.g. `CMS/Internal`).

- `twiki_get_page` — one page as plain text (print cover, stripped navigation); `raw=true` (default) adds footer metadata (revision, author, date, parent, attachments).
- `twiki_list_topics` — topic list of a public web.
- `twiki_download_attachment` — bounded download of a `/twiki/pub/...` attachment URL.

## CMS Online tools (OMS API, cmsoms.cern.ch)

`cmsonline.cern.ch` is an Oracle WebCenter JS portal with no addressable REST surface, so CMS Online operations data is read from the Online Monitoring System (OMS) API instead. CERN-authenticated (SSO session or CERN network; anonymous probes return 401).

- `cmsonline_get_version` — OMS API version (connectivity/auth probe).
- `cmsonline_list_runs` — runs with attributes; optional exact filters (`run_number` ±250 window, `fill_number`), offset paging.
- `cmsonline_get_run` — full attribute record of one run.
- `cmsonline_list_fills` — LHC fills; optional `fill_number_min` lower bound.
- `cmsonline_list_lumisections` — lumisections of one run, offset paging.
- `cmsonline_get_shift_schedule` — shifter grid of one date (ISO `YYYY-MM-DD`): role blocks per system with name, id, start, block length.

## CMS Online E-Log tools (cmsonline.cern.ch portal)

The portal's ELog portlet proxies a DataTables JSON backend at `POST /Elog/json.do`; the elog adapter uses the curated portal POST path (allowlisted to `cmsonline.cern.ch`, read-only semantics) plus the SSO+webcenter session.

- `cmsonline_elog_search` — full-text search (text/subject/author, date range, optional logbook `sub_id`; empty searches all logbooks). Paging via `offset`.
- `cmsonline_elog_get_detail` — full entry by `msg_id` (body text + attachment links).
- `cmsonline_elog_list_logbooks` — E-Log tree ids (`cat_id`/`sub_id`) for scoping searches.
- `cmsonline_elog_download_attachment` — bounded download of `/Elog/htdocs/data/...` or `/Elog/htdocs/struts/download.jsp?...` attachments (saved under the `fName` filename when present).

## CERN Jira tools (its.cern.ch/jira)

Standard Jira Server REST v2. Public projects readable anonymously; restricted
ones need a Jira session cookie. its.cern.ch logs in through a silent
auth.cern.ch OIDC handshake (client `jira-central-prod`): `cern_auth_sign_in`
captures that session, and the adapter re-warms it headlessly when a request
hits 401/403 (the Jira session can expire before the Keycloak identity).
Without a live session, `jira_search` still answers but marks the result with
a public-only `note`.

- `jira_search` — JQL search with `start_at`/`max_results` paging; summary-level fields.
- `jira_get_issue` — full issue by key: description, comments, attachments, people, dates.
- `jira_get_project` — project metadata (lead, components, issue types).

## McM tools (cms-PdmV, cms-pdmv-prod.web.cern.ch)

McM's own **public REST API index** (`/mcm/public/restapi/`) requires no SSO. Full-text search is SSO-only and not exposed; ids come from the caller.

- `mcm_get_request` — full request document by prepid (compact view: approval, status, campaign, events, history).
- `mcm_get_status_and_approval` — approval+status string (e.g. `submit-done`), split into fields.
- `mcm_get_output_datasets` — output dataset names of one request.
- `mcm_find_producing_requests` — exact dataset name → producing request (no wildcards).

## CMS DAS tools (cmsweb.cern.ch/das)

DAS QL via the local `dasgoclient` CLI (`dasgoclient -json`), spawned per call with a timeout and output cap. Auth is a **grid client certificate**, not the SSO session: the runner injects `X509_USER_CERT`/`X509_USER_KEY` from `CMCPS_GRID_CERT_FILE`/`CMCPS_GRID_KEY_FILE` (default `~/.globus/usercert.pem`/`userkey.pem`; a plain `voms-proxy-init` proxy also works). Locate the binary via `CMCPS_DASGOCLIENT_PATH` if it is not on PATH. These two tools are intentionally *not* behind the CERN SSO guard.

- `das_search` — DAS QL: `dataset=/TT*/RunII*/AODSIM`, `block`, `site`, `file`, `run`, `lumi`. Offset paging (`idx`), limit ≤ 50, rows deduplicated per replica; `has_more` from a limit+1 probe (`-json` has no `nresults`, so `total_count` is null).
- `das_get_dataset_info` — exact dataset attribute record (no wildcards).

## goCMS tools (gocms.cern.ch)

**CMS go-link (shortlink) service**, not a docs wiki: FastAPI with an anonymous `OpenAPI` spec. 

- `gocms_list_links` — search/list shortlinks (text search, tag filter, sort, pagination).
- `gocms_get_link` — one shortlink by id.
- `gocms_list_tags` — all tags.

## CMS SDT tools (cmssdt.cern.ch/SDT)

`cmssdt_list_releases` uses the public `ReleasesXML` feed. The IB-dashboard data files and the cms-sw build-status mirrors are OIDC-protected; the shared SSO session covers them (no extra token).

- `cmssdt_list_releases` — announced CMSSW releases per architecture; substring filters (`arch_contains`, `label_contains`, exact `state`), client-side paging.
- `cmssdt_list_ib_queues` — IB queues per release family (from `structure.json`; optional family filter; default queue in `note`).
- `cmssdt_ib_summary` — latest IB name and status per queue/architecture; `queue`/`arch` substring filters, exact `status` (`passed|warning|error`).
- `cmssdt_get_ib_details` — bounded details of one IB comparison (`comparisons` of `<QUEUE>.json`, newest first) selected by `comparison_index` or exact `ib_date`: status fields, list-field counts, merged `cmssw`/`cmsdist` PRs.
- `cmssdt_list_cmssw_labels` — CMSSW GitHub labels and colors (prefix filter).
- `cmssdt_list_open_prs` — open (or any-state) PRs of a release family in `cmssw`/`cmsdist`, filter by repo/state/label, newest first.
- `cmssdt_relval_archs` — architectures with RelVal results for each full IB name (substring filter).
- `cmssdt_build_log_url` — derives the `showBuildLogs` page URL for a full IB name and architecture (no network call; `fwlite` variant supported).

## XSDB tools (xsecdb-xsdb-official.app.cern.ch)

Cross-section record API. Every route is OIDC-gated (verified); a completed `cern_auth_sign_in` session is mandatory. Field details follow the documented XSDB UI surface; live verification is pending an authenticated session.

- `xsdb_search` — records filtered by `particles`, `process_name`, `energy`; pageNum/pageSize paging (no totals).
- `xsdb_list_tables` — linked summary-table names.

## CMS HLT info tools (cmshltinfo-dev.app.cern.ch)

Custom JSON API v0 (routes recovered from the service frontend bundle; see [docs/m3-adapters.md](docs/m3-adapters.md)). The API accepts only a **Keycloak Bearer token** for its own client — SSO cookies return `401 {"status":"Computer says no"}`.

Credential resolution (first match wins):

1. The `cern_auth_sign_in` browser flow opens the HLT app in the same authenticated context and **captures the Keycloak token exchange** (access + refresh token, client id, token endpoint), stored in `cern-service-tokens.json` (0600). Expired tokens are refreshed silently via `grant_type=refresh_token`; no browser needed until the refresh token dies.
2. Fallback: `CMCPS_HLTINFO_TOKEN` manually set to a valid token.

- `hltinfo_list_menus` — HLT menus known to the service.
- `hltinfo_get_path_summary` — per-year path summary table.
- `hltinfo_get_path_names` — path names, optional simple filter.
- `hltinfo_get_path` — full path record (versions, filters, per-run data).
- `hltinfo_get_path_prescales` — prescale values over a run range.
- `hltinfo_get_run_lumis` — run/lumi ranges where the path was enabled.

## CMS Search tools (cms-search.web.cern.ch)

Unified documentation search over ~20 CMS/CERN IT documentation sources ("CERN Batch Docs Search"; public OpenAPI spec). Every route is Keycloak-proxied, so the shared SSO session is mandatory.

- `cmssearch_search` — unified search (`query`, `size` ≤ 50, `offset`, `sort`, `rank`, `date_from`/`date_to`, repeated `indices` filter); returns title, URL, snippet, score, date, tags with total count.
- `cmssearch_get_document` — full indexed document content by `source_key` + `document_id`, optional `offset`/`limit`. **Known backend limitation**: the documents route answers `401 'Invalid internal service credential'` for every source and session; fetch the hit's URL with the matching source tool instead.
- `cmssearch_list_sources` — indexed sources (`source_key`, index name, tags, base URL).
- `cmssearch_index_stats` — document counts and freshness per source, optional `source_key` filter.

## GLANCE / CMSFence tools (cmsfence.cern.ch)

GLANCE exposes separate Keycloak **Bearer-only** APIs per application — browser SSO cookies alone answer 401:

- **ALCM** (CADI/analysis-note registry, audience `cms-alcm-api-prod`)
- **Membership** (audience `cms-membership-api-prod`)

`cern_auth_sign_in` loads each app root during sign-in and **captures its token exchange** (same mechanism as the HLT info token capture); expiring access tokens refresh silently. Manual fallbacks: `CMCPS_GLANCE_ALCM_TOKEN` / `CMCPS_GLANCE_MEMBERSHIP_TOKEN`. If sign-in dates from before this feature existed, re-run it to capture the new audiences.

- `glance_alcm_user`, `glance_membership_user` — current user record in each app.
- `glance_search_analyses` — analysis search with the same fields as the ALCM analysis page (`custom`, `code`, `title`, `stage`, `working_group`, `contacts`, `start_date`/`end_date` on registration date, `sort_by`/`sort_order`); the API has no server search, so filters run client-side over the full list, paginated.
- `glance_get_analysis` — full analysis-page profile by code (`EXO-24-010`) or numeric id: identity/dates/authors/abstract, ARC review committee (chair/LEs/members), paper info, workflow (stage, entry conditions, past events), analysis history, attachments (document versions, GitLab repos, Indico contributions, related-note documents), hashtags, and a derived `links` block (`pubtalk_forum`, `public_result`, `twiki`) built exactly as the page builds them. Support-endpoint failures surface per-section as `{"error": ...}`, never silently.
- `glance_list_published_analyses` — published analyses; filters incl. `has_arxiv`.
- `glance_list_note_types` — note types (Analysis Note, Conference Report, …).
- `glance_search_notes` — search notes by `title_contains`/`working_group`/`type_name`/`project`. The upstream index is one ~15 MB payload fetched in full per call: raise `CMCPS_MAX_RESPONSE_BYTES` above ~16 MB to use it (documented in the tool description; no silent truncation).
- `glance_get_note` — note detail by numeric id.
- `glance_list_projects` — ALCM projects with e-groups.
- `glance_list_alcm_working_groups` — working groups, `kind=physics|non-physics|all`.
- `glance_alcm_members` — ALCM member roster, substring name filter.
- `glance_membership_search_members` — CMS member search by name (`query` required), paginated client-side.
- `glance_download_attachment` — download one GLANCE attachment version (PAPER/PAS drafts, analysis-note PDFs, CWR documents) into CMcpS storage by exact version `filename` (e.g. `HIG-23-004-paper-v10.pdf`). The files are served from `cms-alcm.web.cern.ch` behind SSO **session cookies** (not Bearer tokens): the sign-in cookie capture includes that host, and an HTML login page is detected and reported instead of being saved as a file.

## CMS DocDB tools (cms-docdb.cern.ch)

The CMS Document Database is a modified Perl-CGI DocDB (8.9.0) with an HTML-only surface — no JSON API, and the search form is a POST blocked by the Keycloak fronted proxy ("Invalid Request"); `XSearch` covers *other* DocDB installations only. All tools therefore use parseable GET endpoints, SSO session required throughout. A missing or non-visible document renders an HTTP 200 "Not authorized" page, which `docdb_get_document` maps to a non-retryable error. TLS note: cms-docdb serves a host certificate from the CERN Grid Certification Authority, which is absent from the certifi bundle; the shared client trusts the system CA store for this reason.

- `docdb_get_document` — metadata of one document by numeric docid (e.g. 14372 from a URL), optional older `version`: canonical id (CMS-doc-N-vM), title, doc type, submitter/updater, created/contents-revised/metadata-revised dates, viewable+modifiable groups, abstract, files with exact RetrieveFile download URLs, older-version history, topic paths, authors (name/authorid/institute), keywords.
- `docdb_search` — GET search: full text (`text`, DocDB simple search across title/abstract/keywords/authors) and/or title-only (`title`), optional `updated_from`/`updated_to` (YYYY-MM-DD; mapped onto DocDB's day/month-name/year filter triples). One row per document (docid, canonical, title, first author, topics, last updated); offset/limit paginate client-side over the results table.
- `docdb_list_recent` — documents modified in the last `days` days (DocDB 'see recent'). `ListBy?alldocs=1` exceeds the response policy on the real corpus and is intentionally not exposed.
- `docdb_download_file` — bounded download of one document file (RetrieveFile) by docid + exact `filename` (+optional `version`) taken from `docdb_get_document`.

## CMS HyperNews archive tools (hypernews.cern.ch)

CMS HyperNews was **discontinued at the end of 2022 in favor of CMS Talk** (Discourse, cms-talk.web.cern.ch), with 2022 as the overlap year when both systems ran. What remains at hypernews.cern.ch is a read-only archive (pre-2023 discussion only) served by [HyperNewsViewer](https://github.com/henryiii/hypernewsviewer): server-rendered HTML only, no JSON API, behind an oauth2-proxy fronting CERN SSO — so these tools are explicit HTML parsing over the shared SSO cookie session, and every tool description repeats the discontinuation notice (for 2023+ discussion use `discourse_*` with `site='cms-talk'`). A missing forum/member makes the viewer raise HTTP 500; a missing message inside an existing forum returns HTTP 200 with "Unable to find message" and is mapped to a non-retryable 404-class error.

- `hypernews_search` — full-text search (SQLite FTS5 syntax) over all archived posts, 50 per page, optional `start`/`stop` date filters (YYYY-MM-DD, archive range 2000-01-01..2022-12-31): title, forum, message path, timestamp, author username, snippet.
- `hypernews_list_forums` — all ~2900 forums sorted by last activity, client-side `category` (substring, resolved via the cindex page) and `title_contains` filters; gives the forum names accepted by the tools below.
- `hypernews_get_forum` — top-level threads of one forum: message number, title, author, date, reply count.
- `hypernews_get_message` — one message by forum + slash-separated path (e.g. `788/2`): title, date, author name/username, plain-text body (both marker-wrapped plain-text bodies and bare-HTML bodies), direct replies, attachment URLs, next-in-thread/next-in-forum flags.
- `hypernews_get_member` — member profile by exact case-sensitive username (the viewer route reads the first query-string *key* as the username): display name, email, homepage, status, and all remaining profile fields.
- `hypernews_download_file` — bounded download of a `/HyperNews/CMS/get/AUX/…` attachment into CMcpS storage; archived bodies use both site-relative and absolute, lowercase and uppercase `HREF` forms — all are normalized.

## MkDocs tools (docs.cern.ch and any MkDocs site)

Generic MkDocs (Material) reader: the site search index drives search/listing, pages are fetched as rendered HTML and stripped to plain text. SSO-protected sites (all `*.docs.cern.ch`) work through the SSO cookie session after `cern_auth_sign_in`. `base_url` is the site root (e.g. `https://cms-l1t-correlator.docs.cern.ch`).

- `mkdocs_search` — full-text search over the site search index (title hits weigh 10x; snippets).
- `mkdocs_list_pages` — all page locations/titles, optional substring filter.
- `mkdocs_get_page` — one page as plain text (code/tables preserved).
- `mkdocs_download_file` — bounded download of a static asset (plot/PDF/ROOT file) hosted on a docs site.

## Local PDF figure tools (PyMuPDF, no network)

Local post-processing for PDFs fetched with the `*_download_*` tools (or any local PDF): geometry probing to derive a crop box, vector-preserving cropping, and raster rendering for visual verification. No network I/O; outputs stay inside CMcpS download storage under the usual size caps. Pages are numbered from 1 (pdftotext convention).

- `pdf_probe_page` — text blocks, embedded-image boxes, and union bounds of vector drawings for one page, plus combined `clip_hints`. Check `drawings_union` before cropping: dashed frames/plot axes can extend below the last text block.
- `pdf_crop_figure` — crops one page region into a new single-page PDF via `show_pdf_page`, so drawings stay vector and text stays selectable (no rasterization). Optional plain `output_name`.
- `pdf_render_page` — rasterizes a page or a clipped region to PNG for visual verification (`dpi` 1–600, default 150; re-render higher for pale figures before concluding they are empty).

## Configuration

Configuration uses environment variables with the `CMCPS_` prefix. Common settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CMCPS_STATE_DIR` | `~/.local/state/cmcps` | Local state directory |
| `CMCPS_SSO_START_URL` | `https://indico.cern.ch/user/dashboard/` | Protected page that initiates CERN SSO in the browser |
| `CMCPS_SSO_IDENTITY_HOST` | `auth.cern.ch` | Host whose identity cookie proves login completed |
| `CMCPS_BROWSER_EXECUTABLE` | auto-detected Chromium/Chrome | Browser binary used by Playwright |
| `CMCPS_SSO_TIMEOUT_SECONDS` | `180` | Maximum browser sign-in wait (30–900) |
| `CMCPS_SESSION_MAX_AGE_SECONDS` | `86400` | Maximum local age accepted for browser-session identity cookies |
| `CMCPS_IDENTITY_COOKIE_NAMES` | `["KEYCLOAK_IDENTITY"]` | Cookie names accepted as proof of login |
| `CMCPS_ALLOWED_HOSTS` | CERN hosts and INSPIRE-HEP | Request allowlist |
| `CMCPS_REQUEST_TIMEOUT_SECONDS` | `30` | Upstream request timeout |
| `CMCPS_MAX_RESPONSE_BYTES` | `10485760` | Maximum in-memory response size |
| `CMCPS_MAX_DOWNLOAD_BYTES` | `52428800` | Maximum returned/downloaded file size |
| `CMCPS_REQUIRE_SSO` | `true` | Enforce authentication before protected tools |
| `CMCPS_INDICO_BASE_URL` | `https://indico.cern.ch` | Indico instance used by the Indico tools |
| `CMCPS_INDICO_API_TOKEN` | unset | Indico personal API token (`indp_...`); required for protected events/categories |
| `CMCPS_INSPIRE_BASE_URL` | `https://inspirehep.net/api` | INSPIRE-HEP API root |
| `CMCPS_GITLAB_BASE_URL` | `https://gitlab.cern.ch` | CERN GitLab instance |
| `CMCPS_GITLAB_TOKEN` | unset | Optional GitLab personal access token (`read_api` scope) |
| `CMCPS_CDS_BASE_URL` | `https://cds.cern.ch` | CDS instance |

Service tokens come only from these namespaced environment variables and are injected by the trusted HTTP layer; they are never accepted as tool arguments, mixed into unrelated origins, or written to logs.

`CMCPS_REQUIRE_SSO=false` exists only for offline/public-endpoint development and must not be used as a substitute for the intended CERN flow. Configuration is read from the process environment only; CMcpS does not implicitly trust a `.env` file from an MCP client's launch directory.

The session store and its state directory are written with owner-only permissions. It contains browser cookies and should be treated as a credential file. Logs and tool outputs expose only metadata and redacted URLs, never cookie or authorization values.

## Safety model

- MCP tools are read-only by design.
- The shared HTTP layer accepts only `GET` and `HEAD` requests.
- Requests are constrained to configured host allowlists.
- Authenticated cookies are injected only after RFC 6265 host-only/domain and path-boundary checks.
- Saved browser state is minimized to allowlisted cookies; localStorage origins are never persisted.
- Redirects are bounded and checked again before following.
- Response and download sizes are bounded; downloads stay inside CMcpS state storage.
- Secrets, absolute credential paths, and session payloads are excluded from logs, errors, and tool results.

## Tests

```bash
uv run pytest
uv run ruff check .
```

Live browser automation is not part of the default suite. Unit and integration tests use recorded storage states and mocked network/browser boundaries.

