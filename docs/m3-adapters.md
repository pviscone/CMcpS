# M3 adapter discovery notes

Per-adapter endpoint discovery and contract notes, accumulated as each M3
platform lands. Testing flags: default tests are fixture-only;
`CMCPS_LIVE_PUBLIC=1` / `CMCPS_LIVE_CERN=1` gate live checks.

## Discourse (cms-talk, cms-pub-talk, root-forum, geant4-forum)

- **Endpoint type**: stock Discourse public JSON API (`.json` routes).
- **Auth**: `root-forum.cern.ch` and `geant4-forum.web.cern.ch` anonymously
  readable; `cms-talk.web.cern.ch` and `cms-pub-talk.web.cern.ch` redirect to
  `/auth/oauth2_basic` (CERN SSO) and answer JSON routes with `403` for
  anonymous clients — SSO cookies from `cern_auth_sign_in` cover them.
- **Endpoints**:
  - `GET /search.json?q=...&page=N` — posts+topics (blurb excerpts);
    `grouped_search_result.more_full_page_results` signals next page; **no total
    count**.
  - `GET /latest.json?page=N`, `GET /top.json?period=...&page=N` —
    `topic_list.more_topics_url` signals next page.
  - `GET /c/{category_id}.json?page=N` — one category's topics.
  - `GET /categories.json` — category tree with counts.
  - `GET /t/{topic_id}.json?page=N` — topic metadata + `post_stream.posts`
    (~20 posts per page, `cooked` HTML body, `stream` id list).
  - `GET /posts/{post_id}.json` — one post; anonymous reads on public sites
    include the raw markdown (`raw`).
  - `GET /u/{username}.json` — public profile summary.
  - `GET /uploads/...` — attachments referenced from `cooked` HTML.
- **Normalization**: `cooked` HTML stripped to plain text in-process
  (stdlib `html.parser`); upload links extracted from `href`/`src` pointing at
  `/uploads/` and absolutized against the site base URL.
- **Known limits**: search has no total hit count and no relevance score in
  the lazy format; topic pagination is post-chunk based, not offset-based.

## CMS public results (cms-results.web.cern.ch)

- **Endpoint type**: static machine-generated HTML (documented fallback; no
  JSON API used). The native backend
  `cms-results-search.app.cern.ch/publications` (DataTables API, read token
  embedded in the site's public `bundle.js`) returned persistent
  502/503/401 "application-unavailable" during discovery, so the adapter
  parses the public-results pages instead.
- **Auth**: none (public site).
- **Endpoints** (base `https://cms-results.web.cern.ch/cms-results/public-results/`):
  - `publications/index.html` — working-group index links
    (`public-results/(publications|preliminary-results)/<WG>/index.html`);
    WG codes filtered to `^[A-Z]{2,6}$`.
  - `{publications|preliminary-results}/<WG>/index.html` — one `<tr>` per
    paper inside `<!-- BEGIN <ID> -->` blocks; cells `td.cadi` (paper id +
    link), `td.title`, `td.status` (journal + DOI), `td.date`; sorted by date
    descending in the tool.
  - `<kind>/<ID>/index.html` — detail: `td.titleline`, `td.dateline` ×2
    (collaboration, date), `td.journalline` (DOI), `td.textline` (abstract
    prefixed `Abstract: `), `td.link` (arXiv/PDF/CDS/INSPIRE/twiki/Rivet/
    HepData), figures/tables as same-directory `CMS-<ID>_(Figure|Table)_*`
    files (`-thumb.` images excluded).
- **Normalization**: HTML stripped via shared `adapters/common.html_to_text`.
- **Known limits**: no server-side search; `year`/`title_contains` filters
  are client-side; listing is the full group table (no pagination needed).

## TWiki (twiki.cern.ch)

- **Endpoint type**: TWiki CGI scripts; HTML parsing of rendered pages and
  raw wiki text through `?raw=on`.
- **Auth**: anonymous `bin/view` works for public webs (verified on
  `CMSPublic`); `bin/search`, `?raw=on`, and restricted webs redirect to
  Keycloak OIDC (`auth.cern.ch`, client `twiki-production`) — served by the
  SSO session. Auth-substituted HTML is detected and raised as a structured
  upstream error with sign-in instructions.
- **Endpoints**:
  - `GET /twiki/bin/search/<Web>/?search=...&type=keyword|literal|regex&limit=N`
    — results rendered as `bin/view` links; extracted with anchor text,
    deduplicated. Multiple webs in one search are passed as
    `?web=Web1,Web2` (TWiki VarSEARCH comma-list; first web stays in the
    URL path); tool default is `CMS,CMSPublic`.
  - `GET /twiki/bin/view/<Web>/<Topic>?raw=on` — raw page; `%META:TOPICINFO`
    (author/date/version), `%META:TOPICPARENT`, `%META:FILEATTACHMENT`
    (name/size/comment) parsed; body = raw wiki text minus META lines.
  - `GET /twiki/bin/view/<Web>/<Topic>` — rendered HTML → plain text.
  - `GET /twiki/bin/view/<Web>/WebTopicList` — topic index of the web;
    entries filtered to `[A-Za-z0-9]`-initial names.
  - `GET /twiki/pub/<Web>/<Topic>/<file>` — attachments; bounded download.
- **Known limits**: search returns topic references only (no snippets);
  private webs need SSO; legacy URL patterns preserved through the shared
  base URL.

## CMS Online — OMS API (cmsoms.cern.ch)

- **Endpoint type**: OMS JSON API (JSON:API-style envelope).
- **Discovery**: `cmsonline.cern.ch` is an Oracle WebCenter/ADF portal —
  every URL returns the same JavaScript bootstrap with no addressable REST
  surface, so the portal itself is not adapterable. CMS Online
  detector/status/operations data is served by the Online Monitoring System
  at `https://cmsoms.cern.ch/agg/api/v1/` (anonymous probe → `401
  Unauthorized`; reached via the injected SSO session or the CERN network).
- **Endpoints**:
  - `GET /agg/api/v1/version` — API version.
  - `GET /agg/api/v1/runs?filter[run_number][GEQ/LEQ/EQ]=..&filter[fill_number][EQ]=..&page[limit/offset]=..`
  - `GET /agg/api/v1/fills?filter[fill_number][GEQ]=..&page[..]=..`
  - `GET /agg/api/v1/lumisections?filter[run_number][EQ]=..&page[..]=..`
- **Envelope**: `data[]` rows (`id`/`type`/`attributes`),
  `meta.totalResourceCount`, offset/limit pagination normalized into
  `SearchResult`. Run-number exact filters widen to a ±250 window client-side
  (OMS has no single-run endpoint).
- **Known limits**: authenticated endpoint — unauthenticated live checks
  were impossible during discovery; behavior is fixture-verified. No
  download surface (JSON only).

## CERN Jira (its.cern.ch/jira)

- **Endpoint type**: standard Jira Server REST API v2.
- **Auth**: `serverInfo` and public `search` readable anonymously
  (verified); restricted projects/issues need a Jira session cookie.
  its.cern.ch authenticates via a silent auth.cern.ch OIDC handshake
  (client `jira-central-prod`); the sign-in flow captures the session on
  `/jira/login.jsp` (its inline script advances to the IdP), and the adapter
  re-warms it headlessly after a 401/403 —
  Jira session cookies expire before the Keycloak identity. `search` first
  probes `/myself`; without a live session the `SearchResult.note` flags
  public-only visibility.
- **Endpoints**:
  - `GET /jira/rest/api/2/search?jql=...&startAt=&maxResults=&fields=...`
    — `{issues, total, startAt, maxResults}` envelope; offset paging.
  - `GET /jira/rest/api/2/myself` — session probe (401 when anonymous).
  - `GET /jira/rest/api/2/issue/<KEY>?fields=...` — detail with
    plain-text description (Jira Server, not ADF), comments (capped at 50,
    4000 chars each), attachment URLs.
  - `GET /jira/rest/api/2/project/<KEY>` — project metadata (lead,
    components, issue types).
- **Known limits**: no attachment download tool yet (URLs returned, may
  require SSO to fetch); description truncated at 20000 chars.

## McM (cms-pdmv-prod.web.cern.ch/mcm)

- **Endpoint type**: public REST API, self-documented index at
  `/mcm/public/restapi/` (no SSO).
- **Endpoints used** (all verified live):
  - `GET requests/get/<prepid>` — full doc in `{"results": {...}}`; unknown
    ids return `{"results": {}}` (200) → detected and raised as
    not-found `UpstreamError`.
  - `GET requests/get_status_and_approval/<prepid>` — `{"<prepid>":
    "submit-done"}`.
  - `GET requests/output/<prepid>` — `{"<prepid>": [datasets...]}`.
  - `GET requests/produces/<path:dataset>` — dataset **without leading
    slash**; full producing request doc in `results` (empty when no match).
- **Not implemented (verified broken/SSO)**: `from_dataset_name` 404s on
  slash-containing names; `chained_requests/get` 404s even for real ids;
  `/mcm/restapi/...` (search) is SSO-only.

## CMS DAS (cmsweb.cern.ch/das)

- **Endpoint type**: DAS REST (`GET /das/request?view=list&limit&idx&input&
  instance`). Anonymous probe → 401 (SSO required).
- **Response**: `{status, mongo_query, nresults, data}`; `data[i]` keyed by
  entity (`dataset`/`block`/`file`/`site`/`run`/`lumi`), value = attribute
  list; replica duplicates deduplicated by primary `name` key.
- **Known limits**: limit capped at 50 (DAS throttles); fixture-tested,
  live check pending SSO.

## goCMS (gocms.cern.ch)

- **Endpoint type**: FastAPI, public OpenAPI spec at `/openapi.json`. NB:
  gocms.cern.ch is the **CMS shortlink ("go link") service**, not the MkDocs
  documentation wiki assumed in the plan; there is no docs-search surface
  here.
- **Endpoints** (all anonymous, verified live): `GET /api/links` (search,
  tags, page/per_page/sort; envelope `{items,total,page,per_page,
  total_pages}`), `GET /api/links/{id}`, `GET /api/tags`. Mutating routes
  exist in the spec but are never called (GET-only safety layer).

## CMS SDT (cmssdt.cern.ch/SDT)

- **Endpoint type**: legacy CGI export `GET /SDT/cgi-bin/ReleasesXML`
  (public; ~20 architectures, ~1200 project entries). Parsed with
  `defusedxml`, filtered/paged client-side.
- **SSO-only / removed**: `jenkins-artifacts` (OIDC), classic
  `showIB.html`/`showmatrix.py` → 404. The IB dashboards themselves were
  implemented later — see "CMS SDT IB dashboards" below.

## XSDB (xsecdb-xsdb-official.app.cern.ch)

- **Endpoint type**: JSON API behind Keycloak OIDC (every anonymous request
  302s to `auth.cern.ch`, including `/api/search` and
  `/api/getLinkedTableNames`; existence confirmed, full field semantics
  verified by route docs/UI only).
- **Endpoints**: `GET /api/search?<filters>&pageNum&pageSize` (bare JSON
  list, no totals; fullness paging) with UI filter names (`particles`,
  `process_name`, `energy`, ... allowlist), `GET /api/getLinkedTableNames`.
- **Known limits**: live behavior pending an authenticated SSO session;
  OIDC HTML in place of JSON is caught by the JSON-decoder guard.

## CMS HLT info (cmshltinfo-dev.app.cern.ch)

- **Endpoint type**: custom JSON API v0, routes recovered from the service's
  own frontend bundle (`assets/APIServices-*.js`): `hltmenus` (→ `{"menus"}`),
  `pathsummary`, `path/{name}`, `pathnames[/{filter}]`,
  `prescales/path/{name}?minrun&maxrun`, `prescales/path/{name}/runlumis`
  (all → `{"result"}`). GET-only, JSON-only.
- **Auth**: every `/api/v0/*` route answers anonymous requests
  `401 {"status":"Computer says no"}` (custom handler, same on the prod
  `cmshltinfo.app.cern.ch` sibling). The SPA sends
  `Authorization: Bearer <keycloak token>` for its own public client;
  browser-SSO cookies do not substitute.
- **Credential capture (implemented)**: `cern_auth_sign_in` opens the app
  root in the already-authenticated context and intercepts the SPA's
  authorization-code exchange with the Keycloak token endpoint
  (`/protocol/openid-connect/token` POST, captured via a Playwright
  response listener). Access + refresh tokens, client id, and endpoint are
  stored in `cern-service-tokens.json` (0600, schema v1, `auth/tokens.py`).
  `AuthService.service_bearer()` returns a valid access token, refreshing
  via `grant_type=refresh_token` when expired and persisting the refresh.
  Capture failure is non-fatal for sign-in; `CMCPS_HLTINFO_TOKEN` remains
  the manual fallback. Adapter header policy = captured token, then env
  token.
- **Known limits**: if the SPA changes its OIDC handshake or the refresh
  token is revoked, re-run `cern_auth_sign_in`. No search beyond
  substring `pathnames/{filter}`.

## MkDocs (docs.cern.ch fleet and generic MkDocs sites)

- **Endpoint type**: stock MkDocs/Material static output; live discovery on
  `cms-l1t-correlator.docs.cern.ch` (all `*.docs.cern.ch` homes are
  Keycloak-proxied — anonymous requests get the `login-pf` HTML; the shared
  SSO cookie session unlocks JSON/HTML fully).
- **Endpoints**:
  - `GET <base>/search/search_index.json` — `{config, docs:[{location,
    title, text, tags?}]}`; client-side scoring (title hits 10x, per-term
    substring counts) with snippets.
  - `GET <base>/<location>` — page HTML → plain text via shared extractor.
  - `GET <base>/<asset>` — bounded downloads, host-validated.
- **Design note**: no raw-markdown endpoint exists in stock MkDocs; this
  matches the `mkdocs-mcp` reference approach (search index + page fetch).
  SSO HTML is sniffed (`login-pf` / "Sign in to CERN" markers) and reported
  with sign-in instructions. Page location paths are restricted (no `..`,
  scheme, or leading-slash escapes).
- **Known limits**: page text includes site chrome (nav/footer); DNS for
  some docs.cern.ch hosts is flaky from outside CERN (transient
  connect errors are retryable upstream errors).

## CMS Search (cms-search.web.cern.ch) — delivered

- **Endpoint type**: FastAPI ("CERN Batch Docs Search"), public OpenAPI spec
  at `/openapi.json`. Every route proxies through Keycloak — anonymous
  callers get the login page; the shared SSO cookie session works.
- **Endpoints** (verified live with an authenticated session):
  - `GET /search?q&size&from&sort&date_from&date_to&indices&rank` →
    `{query, count, total, offset, limit, rank, results:[{document_id, score,
    index, base_key, tags, title, location, url, snippet, date}]}`.
    `indices` is a repeated parameter. `rank` values are backend-defined
    (default e.g. `relevance_pagerank_recent`) and passed through.
  - `GET /api/documents/{source_key}/{document_id}?offset&limit` — document
    content; response shape not probed live, adapter passes any top-level
    mapping through under `document` (structure-tolerant).
  - `GET /api/docs-config` → `{docs:{<source_key>:{index_name,tags,url}}}`
    (~20 CMS/CERN IT sources).
  - `GET /api/index-stats` → `{stats:{<source_key>:{count,last_updated_at,
    last_updated_source,latest_document_date}}}`.
- **Tools**: `cmssearch_search`, `cmssearch_get_document`,
  `cmssearch_list_sources`, `cmssearch_index_stats`. Base URL is a module
  constant in the adapter (`*.cern.ch` already allowlisted).

## GLANCE/CMSFence (cmsfence.cern.ch) — delivered

Previously deferred: every route was Keycloak-proxied and no session existed.
Authenticated discovery (browser network inspection) found the mechanism:

- **Auth**: three SPAs (`/alcm`, `/membership`, `/incubator`). Each SPA runs
  a Keycloak PKCE flow then calls its API with a **Bearer** token whose
  audience is `cms-<app>-api-prod`; SSO cookies, or the other app's token,
  answer 401. Tokens are captured automatically by `cern_auth_sign_in`
  through `BEARER_SERVICES` (same mechanism as hltinfo — app-root load
  triggers the exchange, access+refresh stored in
  `cern-service-tokens.json`, silent refresh). Manual env fallbacks:
  `CMCPS_GLANCE_ALCM_TOKEN`, `CMCPS_GLANCE_MEMBERSHIP_TOKEN`.
- **ALCM (CADI) endpoints verified live**: `GET /alcm/api/user`,
  `/alcm/api/analysis` (internal list `{analysis:[...]}`, ~1.7 MB),
  `/alcm/api/public/analysis` (`{analyses:[...]}`), `/alcm/api/notes`
  (`{notes:[...]}`, single ~15 MB payload — needs
  `CMCPS_MAX_RESPONSE_BYTES` > ~16 MB), `/alcm/api/notes/{id}`,
  `/alcm/api/notes/types`, `/alcm/api/projects`,
  `/alcm/api/working-groups` (non-physics), `/alcm/api/physics-working-groups`,
  `/alcm/api/members` (~1.1 MB roster), `/alcm/api/analysis/{id}` and
  `/alcm/api/analysis/ancode/{code}` — full analysis detail (proponents/
  contacts with emails, reading institutions + team leaders, `ircInvitations`
  institutional-review invitations). Details-page support endpoints (verified
  live on EXO-24-010): `/alcm/api/analysis/{id}/review-committee` (ARC:
  chair/le/members + egroup), `/alcm/api/analysis/{id}/paper` (journal,
  EP number, authorlist status), `/alcm/api/transitions/{id}` (workflow
  stage, entry conditions, past events), `/alcm/api/analysis/{id}/history`,
  `/alcm/api/attachments/{documents,gitlab-repositories,indico-contributions,
  paths}/entity/{id}` (+`/alcm/api/attachments/tags`),
  `/alcm/api/hashtags/entity/{id}`; per-related-note documents are fetched
  via `/alcm/api/attachments/documents/entity/{note_id}`. The page's "Pubtalk
  Forum" and "Public
  Result" links are pure client-side derivations —
  `https://cms-pub-talk.web.cern.ch/c/{WG}/{paperIdentifier}` and
  `https://cms-results.web.cern.ch/cms-results/public-results/{preliminary-results|publications}/{paperIdentifier}/`
  (verbalized against live DOM hrefs) — exposed under `links` in
  `glance_get_analysis`. Search note: the analysis-page URL params
  (custom/code/c_title/c_stage/c_wg/c_contacts/startDate/endDate/sortBy/
  sortOrder) are SPA-side filters — the API ignores them, so tools apply the
  same filtering client-side.
- **Membership endpoints verified live**: `/membership/api/user`,
  `/membership/api/members/search?q=`. `/members`, `/members/{id}`, `/teams`,
  `/managers` are 404/500 — deliberately not exposed.
- **Tools** (12): `glance_alcm_user`, `glance_membership_user`,
  `glance_search_analyses`, `glance_list_published_analyses`,
  `glance_get_analysis`, `glance_list_note_types`, `glance_search_notes`, `glance_get_note`,
  `glance_list_projects`, `glance_list_alcm_working_groups`
  (kind=physics/non-physics/all), `glance_alcm_members`,
  `glance_membership_search_members`. Filters are client-side where the
  upstream has none; output projected to verified fields.
- **Not implemented**: incubator SPA (`vocms0705`) — nothing useful
  read-only verified. GLANCE's machine API for third parties also exists
  (client-credentials flow + per-audience authorization ticket via a
  CMSGLANCE Jira request — see the internal API guide reachable only via
  SSO); the captured user Bearer tokens cover the same audiences without
  extra registration.

## CMS SDT IB dashboards (cmssdt_* extension) — delivered

- **Auth**: dashboard data files are OIDC-protected (anonymous 302 →
  Keycloak); the shared SSO cookie session covers them. `showBuildLogs.py`
  and `ReleasesXML` are public.
- **Endpoints** (all verified live via authenticated session):
  - `GET /SDT/html/data/structure.json` — `{<family>: [<queues>],
    all_prefixes, all_release_queues, default_release}`.
  - `GET /SDT/html/data/LatestIBsSummary.json` —
    `{<queue>: {<arch>: {latest_IB, status}}}`.
  - `GET /SDT/html/data/<QUEUE>.json` (~380 KB) — `{base_branch,
    release_name, comparisons:[{ib_date, isIB, inProgress, next_ib, status
    strings (check-headers, clang_analyzer, …), list fields (builds,
    relvals, utests, addons, fwlite), merged_prs, cmsdist_merged_prs,
    cmsdist_compared_tags, comp_baseline(_state)}]}` newest first.
  - `GET /SDT/public/cms-sw.github.io/data/cmssw_labels.json` —
    `{label: color}`.
  - `GET /SDT/public/cms-sw.github.io/data/prs/<FAMILY>.json` —
    `{cmssw|cmsdist: {<pr_number>: {title, author, state, labels, branch,
    unix-second timestamps, ...}}}`.
  - `GET /SDT/public/cms-sw.github.io/data/RelvalsAvailableResults.json` —
    `{<full_IB>: "arch[,arch…]"}`.
  - Build logs (public): `GET /SDT/cgi-bin/showBuildLogs.py[/fwlite]/<arch>/
    www/<day>/<queue_dir>/<full_IB>`; `<queue_dir>` =
    `<M>.<m>[.<variant>…]-<weekday>-<hour>` — ground truth live-verified:
    `20.1-thu-11` (CMSSW_20_1_X_2026-10-22-1100),
    `20.1.CPP23-thu-11` (CMSSW_20_1_CPP23_X_2026-10-22-1100).
- **Tools** (8 total): existing `cmssdt_list_releases` plus
  `cmssdt_list_ib_queues`, `cmssdt_ib_summary`, `cmssdt_get_ib_details`,
  `cmssdt_list_cmssw_labels`, `cmssdt_list_open_prs`, `cmssdt_relval_archs`,
  `cmssdt_build_log_url` (pure derivation, no network).
