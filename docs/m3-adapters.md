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

- **Endpoint type**: TWiki CGI scripts; HTML parsing of the print-cover
  render (`cover=print`, low-noise).
- **Auth**: anonymous `bin/view` renders pages for every web, restricted
  ones included (verified 2026-10 on `CMS` and `CMS/Internal`). CERN TWiki
  no longer serves the wiki source: `?raw=on` returns the rendered page
  (200, HTML) for anonymous AND authenticated sessions, so the adapter
  always consumes the render. Sign-in still visits `bin/viewauth` (silent
  auth.cern.ch handshake drops the TWiki session cookie) so scripts that do
  gate on login (protected attachment downloads) work through the SSO jar.
- **Endpoints**:
  - `GET /twiki/bin/view/<Web>/<Topic>?cover=print` — print-cover render;
    body converted to plain text; footer metadata parsed:
    `Topic revision: r<n> - YYYY-MM-DD - <user>` (version/date/author),
    `This topic:` breadcrumb (immediate parent), and the marked
    `twikiAttachments` section (attachment names/URLs).
  - `GET /twiki/bin/view/<Web>/WebTopicList` — topic index of the web;
    entries filtered to `[A-Za-z0-9]`-initial names.
  - `GET /twiki/pub/<Web>/<Topic>/<file>` — attachments; bounded download.
- **Known limits**: page content is anonymous everywhere; only attachment
  downloads in protected webs need the SSO TWiki session. The headline
  metadata comes from the rendered footer, not `%META` records (raw source
  is not served), so entries missing the footer report nulls. The TWiki CGI
  full-text search
  (`bin/search/<Web>/`) is not exposed — it is slow/flaky (frequent >30 s
  reads) and returns no snippets; search TWiki content through cmssearch
  indices `cms-twiki` and `cms-public-twiki` instead.

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

## CERN Mattermost (mattermost.web.cern.ch) — delivered

- **Endpoint type**: documented public **Mattermost REST API v4** under
  `/api/v4` (CERN fork, observed `x-version-id: 11.7.12...cern`; verified
  2026-10).
- **Auth**: every API endpoint is login-required — anonymous
  `GET /api/v4/users/me` answers JSON `401
  {"id":"api.context.session_expired.app_error"}` (verified). The session
  credential is the `MMAUTHTOKEN` cookie, minted by the OAuth handshake at
  `GET /oauth/gitlab/login` → 302 `mm-auth.web.cern.ch/cern-prod/oauth/
  authorize` (client `mattermost-sso`, cookie `MMOAUTH` en route) →
  `auth.cern.ch` → `/signup/gitlab/complete`; mm-auth brokers to auth.cern.ch
  (verified), so the stored KEYCLOAK_IDENTITY completes the whole chain
  silently. Registered as a `_COOKIE_ONLY_SITES` entry with an
  `MMAUTHTOKEN`-readiness check; the shared client's lazy warm covers both
  GETs and the search POST. Optional fallback: `CMCPS_MATTERMOST_TOKEN`
  personal access token as `Authorization: Bearer` (operator env only).
- **Endpoints used**:
  - `GET /api/v4/users/me` (session probe), `GET /api/v4/users/{id}`,
    `GET /api/v4/users/username/{name}`.
  - `GET /api/v4/users/me/teams` (array payload). CERN-fork quirk
    (verified): `page`/`per_page` are ignored and the full list is
    returned in one call — the tool exposes no paging.
  - `GET /api/v4/users/me/teams/{team_id}/channels`.
    CERN-fork quirk (verified): `page`/`per_page` are ignored and the
    **account-wide** channel list is returned, mixing in every direct (D)
    and group (G) conversation of the user (`team_id` empty) — the adapter
    filters to the requested team's channels client-side so the user's DM
    graph is never echoed; the tool exposes no paging.
  - `GET /api/v4/teams/{team_id}` — team detail; also resolves the team URL
    name for channel/permalink links.
  - `GET /api/v4/channels/{id}` /
    `GET /api/v4/teams/{team_id}/channels/name/{name}`.
  - `GET /api/v4/channels/{id}/posts?page=&per_page=&before=&after=` —
    `{order, posts, prev_post_id, next_post_id}`. Verified semantics:
    `prev_post_id`/`next_post_id` are the boundary ids toward older/newer
    history (`next_post_id` == the `before` cursor used to arrive); on the
    newest page `next_post_id` is empty even when older posts exist, so
    `has_more` ⇔ `prev_post_id` non-empty. Deeper history:
    `before=<oldest post id of the current page>` (excludes itself —
    hole-less).
  - `GET /api/v4/posts/{id}/thread` — `{order, posts}`; the server order is
    **not** time-sorted (verified), so the adapter returns the root post
    first with replies sorted by creation time.
  - `POST /api/v4/teams/{team_id}/posts/search` — the only POST:
    `{terms, is_or_search, include_deleted_channels, time_zone, page}`.
    **Verified quirks on the CERN fork**: cookie-authenticated
    POSTs 401 instantly unless `X-Requested-With: XMLHttpRequest` is sent
    (same `api.context.session_expired` id as a truly missing session);
    results clamp to exactly 100 hits in one page regardless of
    `per_page` (5/60/200 → 100 — the parameter is not sent),
    `page` advances through the capped window and an empty page means
    the end; `matches` is `null`; no hit count. Read-only semantics
    (same precedent as the ServiceNow portal search; allowlisted in the
    safety layer, which now also warm-retries JSON POSTs on 401/403 for
    mapped sites). `has_more` ⇔ page reached the 100-hit cap.
  - `GET /api/v4/files/{id}/info` + `GET /api/v4/files/{id}` — file
    metadata then bounded download (server filename reduced to its basename
    before writing into CMcpS storage).
- **Normalization**: epoch-millisecond timestamps → ISO-8601; post items
  carry `user_id` (not usernames — no N+1 user lookups) and `file_ids`;
  permalinks (`/{team}/pl/<post_id>`) only where the team is known from
  the call context. 401 → `AUTH_REQUIRED`; 403 stays an upstream permission
  error (Mattermost uses it for team/channel access denial while logged
  in).
- **Known limits**: team listing covers membership only (public-team
  discovery not exposed); channel `name_contains` filters the fetched page
  client-side; no reaction/emoji surface; no pinned-post endpoint.

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
    content. **Known backend limitation** (verified 2026-10): answers
    `401 {"detail": "Invalid internal service credential."}` for every
    source and session (anonymous and fresh SSO alike); the site's SPA never
    calls this route. The adapter surfaces that case explicitly and directs
    to the hit's source URL via the matching adapter tool.
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
  answer 401. Tokens are minted lazily on first API use through
  `BEARER_SERVICES` (same mechanism as hltinfo — a headless app-root load
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
- **Document files** (papers/PAS/AN drafts, CWR documents): the attachment
  *metadata* (with version lists) comes from the Bearer API above, but the
  *files themselves* are served from `cms-alcm.web.cern.ch` —
  `host + filePath + / + fileName + . + fileExtension`, exactly the
  `getCernboxLink` helper of the ALCM SPA. That host is a WebFrameworks OIDC
  app: **session-cookie auth only**, no read API, Bearer tokens answer 401.
  The `cern_auth_sign_in` cookie capture includes both
  `cms-alcm.web.cern.ch` and `cds.cern.ch` (restricted CDS records and
  attachments — e.g. CWR paper drafts — work through the same cookie
  session). `glance_download_attachment` probes the download head and
  refuses to save an HTML login page as a file, pointing at re-sign-in.
- **Tools** (13): `glance_alcm_user`, `glance_membership_user`,
  `glance_search_analyses`, `glance_list_published_analyses`,
  `glance_get_analysis`, `glance_list_note_types`, `glance_search_notes`, `glance_get_note`,
  `glance_list_projects`, `glance_list_alcm_working_groups`
  (kind=physics/non-physics/all), `glance_alcm_members`,
  `glance_membership_search_members`, `glance_download_attachment`.
  Filters are client-side where the
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

## CMS DocDB (cms-docdb.cern.ch) — delivered

- **Discovery date**: 2026-10-26 (authenticated probes against the live site).
- **Surface**: modified Perl-CGI DocDB 8.9.0, HTML only; no JSON API.
- **TLS**: cms-docdb serves a **CERN Grid Certification Authority** host
  certificate, which is absent from the certifi root bundle that httpx uses
  by default. `SafeHttpClient` now builds its default client with the
  system CA store (`ssl.create_default_context()`); without this every
  request dies with `[SSL: CERTIFICATE_VERIFY_FAILED]`.
- **Auth**: every page requires the CERN SSO session (anonymous → 302 to
  Keycloak). The shared cookie session suffices.
- **Endpoints** (all verified live):
  - `GET /cgi-bin/DocDB/ShowDocument?docid=N[&version=M]` — full metadata
    page: `<title>CMS-doc-N-vM: …</title>`, `#DocTitle h1`, `#BasicDocInfo`
    `<dl>` (Document #, type, submitter/updater, created/contents-revised/
    metadata-revised), `#Viewable`/`#Modifiable` groups, `#OtherVersions`
    history table, `#Abstract`, `#Files` (RetrieveFile links, description,
    size), `#Topics` hierarchical anchor paths, `#Authors`, `#Keywords`.
    A missing or non-visible document returns HTTP 200 with title
    "Not authorized" and no `#RevisionInfo` block — mapped to a
    non-retryable not-found error.
  - `GET /cgi-bin/DocDB/Search?simpletext=Q&simple=1` — full-text search
    (DocDB "simple search": title/abstract/keywords/authors/publication
    info; plain docid also matches). `titlesearch=…&titlesearchmode=allword&
    mode=title` restricts to the title. Update-date filters require the
    full `afterday`/`aftermonth`(3-letter)/`afteryear` triple (and the
    `before` counterpart); a bare year is silently ignored (0 hits).
    Results render as the `#DocumentTable` HTML table (docid, title anchor,
    first author + "et al.", topics, date) plus "Number of documents
    found: N"; offset/limit slicing is client-side. The plural-input POST
    form (`action=Search`, multipart) is blocked by the Keycloak fronted
    proxy ("Invalid Request"), so GET is the only usable search path.
    `XSearch` queries *other* DocDB installations and shows only public
    documents of those — useless here.
  - `GET /cgi-bin/DocDB/ListBy?days=N` — "see recent" table (same row
    shape). `ListBy?alldocs=1` exceeds `CMCPS_MAX_RESPONSE_BYTES` (10 MB)
    on the live corpus and is intentionally not exposed. `ListAuthors`
    (~620 KB) and `ListTopics`/`DocumentDatabase` (~160 KB) exist but add
    no search value over `docdb_search`; not implemented.
  - `GET /cgi-bin/DocDB/RetrieveFile?docid=N&filename=F[&version=M]` —
    file download into bounded CMcpS storage (verified: fetch of the
    LAGG rules PDF, docid 14372, 315 kB).
- **Tools** (4): `docdb_get_document`, `docdb_search`, `docdb_list_recent`,
  `docdb_download_file`.
- **Not turned down lightly**: DocDB search POST endpoint — read-only
  semantics in principle, but the Keycloak proxy rejects it outright;
  keeping it out also preserves the GET-only guarantee.

## CERN EDMS (edms.cern.ch) — delivered

- **Discovery date**: 2026-10-27 (authenticated probes through the stored
  SSO session; API docs found via the official EDMS FAQ).
- **Surface**: documented JSON REST WS ("EDMS REST WS 1.0.0", apidoc 0.29.0
  generated 2026-10-01) under `/ws/api`; entry point
  `https://edms.cern.ch/ws/apidoc/index.html` (itself SSO-gated). No HTML
  parsing needed.
- **Auth**: `/ws` is an OAuth2 client (`edms_oidc_ws_prod`) fronted by CERN
  Keycloak; anonymous requests 302 into the silent OIDC handshake, which the
  shared cookie session completes transparently — no `_COOKIE_ONLY_SITES`
  entry, no bearer exchange (verified live).
- **Endpoints** (all verified live):
  - `GET /ws/api/users/current` — SSO caller's EDMS identity (uic, username,
    email, display name).
  - `GET /ws/api/documents/{number}[/{version}]` — document detail; version
    optional (latest), the literal `LAST_RELEASED` resolves (doc 188033 →
    v0). Fields: title, authors, dates, context, type, releaseProc,
    status(+color), visibility, keywords, files (name/size/lastModified*,
    optional `convertedPdfUrl`). Unknown documents answer **HTTP 400** with a
    JSON `{"exitInfo": [...]}` body — never 404 (verified for
    `99999999999/1`). Visibility-RESTRICTED documents are masked exactly
    the same (verified: AT-MEL traveller 319622/1, listed by the
    parent-scoped search, detail denied) — the adapter maps both to a
    non-retryable "not found or not visible" error. Document numbers up to
    ~10 digits.
  - `GET /ws/api/documents?<filters>` — PARENT-STRUCTURE listing, not a
    global filter search: `parentObjectType/parentObjectId[/
    parentObjectVersion]/assetAndJobs` scope it (verified: `A` +
    `HCMB__A025-10004120` → 1 doc; `D` + doc id → empty), and
    `type/attributes/context/status/releaseProcedure/obsolete` narrow it
    further (verified: `status=In Work` keeps the doc, `status=Released`
    drops it). **Without a parent scope every filter combination returns
    EMPTY** for an ordinary account (verified: status, type, context,
    obsolete, all 0 hits). Envelope is `{"exitInfo": [], "documents":
    [...]}` (the apidoc sample omits the outer object); no full-text
    parameter, no server-side paging or totals → client-side offset/limit.
    The listing exposes metadata (title etc.) of documents the caller
    cannot open in detail (see the 319622 masking above).
  - `GET /ws/api/files/{number}/{version}/{fileName}` — raw file download
    (verified: doc 1525125 v5 `Searching_EDMS.pdf`, 1.6 MB); `LAST_RELEASED`
    accepted for the version.
  - `GET /ws/api/documentTypes?contextId=<id>[&cadimType=…]` — document
    types per context; **contextId is mandatory** (400 "contextId parameter
    is mandatory") although the apidoc marks every parameter optional
    (verified: `ORG-003107` + `TEXTFILE` → 14 types).
  - `GET /ws/api/contexts` — **broken upstream**: HTTP 500 for every
    parameter combination (bare and all four documented `template` values;
    cross-checked through a full browser session). Not exposed; context
    names/ids (e.g. `EDMS-SERVICE-PUBLIC`, `PLM`, `ORG-003107`) are read
    from document details.
- **UI full-text search** (`edms_search_text`, 2026-10-27): the REST WS
  has no text search and the UI's own search is GWT-RPC (POST, obfuscated
  per redeploy), so the only read-only text search is scraping the UI
  itself. The results page is **deep-linkable**:
  `/ui/#!master/search/view?objecttype=document&query=<q>&latestversion=
  false&hideobsolete=true&dadvanced=false&global=true` (verified: typing
  "ECAL milestones" navigates to exactly this fragment). Flow implemented
  in `adapters/edms_ui.py` on top of the new
  `auth/browser.py:fetch_page_html_text` (HTML+text snapshot, optional
  one-shot `interact`, `settle_seconds`): render → wait for the "Per page"
  marker → settle 5 s (the GWT shell paints the chrome before the grid
  hydrates — without the settle the scrape catches an empty grid) → parse
  100-row document grid + section totals ("Docs & Files (100+)", Projects,
  Items, Assets, F. Positions, Systems, Locations). Anonymous pages still
  show the markers, so login is detected from the banner's "Sign in"
  anchor and a second pass forces the interact (sign-in click completes
  silently; the UI session cookie then persists in the stored state and
  later searches take the fast path). Rows dedup key = `<code> v.<version>`;
  numeric document ids come from the `/file/<id>/<version>/<name>` anchors
  or numeric id cells ("3481902 v.0.1"); formal codes (CMS-EC-SG-0004)
  surface as `code`. Verified live: 'ECAL milestones' → 64 items page 1
  (top hit EDMS 350221 v.6), 9 s warm / 22 s cold (with silent sign-in);
  'tracker cooling' → 61 items. Fragility: coupled to GXT classes
  (`x-grid3-row`, `x-grid3-td-*`, `SearchColumnRenderer*.teaser`) — a UI
  redeploy can break parsing (fails empty, never mis-parses; fixture
  `tests/fixtures/edms_ui_search.html` is a captured real page).
  **Accessibility IS visible in the grid** (first pass missed it, user
  caught it): logged-in pages mark restricted rows with classes
  `SearchColumnRendererHelper-title-accessDenied` (title block),
  `AnchorNoAccess` (grey title anchor) and an icon `title="Access denied"`
  (red circle-minus). Anonymous pages carry none of these — the generic
  "You are not logged in" tooltip there is unrelated. Cross-validated
  against the masked REST probe on live pages both ways
  (marker ⇔ denied, no-marker ⇒ accessible on a 69/31-row page), so the
  parser flags `accessible=false` free of requests. Rows without the
  marker are openable in practice but strictly only the REST detail probe
  (401-masked 400) proves it: `check_access`/`access` probes cover exactly
  those (8-way concurrent, ~+10 s/page). The search index stays global
  metadata — it lists restricted docs (98/98 AT-corpus traveller hits
  listed, all denied), which is why this flag exists at all.
- **Tools** (6): `edms_search_text`, `edms_search_documents`,
  `edms_get_document`, `edms_download_file`, `edms_list_document_types`,
  `edms_get_current_user`.
- **Not turned down lightly**: the contexts endpoint (upstream 500 on every
  variant); assets, config profiles/properties, document-type attributes,
  equipment codes, release procedures, visibilities, structure views,
  thumbnails (document-creation UX, low read value); all POST/PUT routes
  (create document/version, status changes, attach/detach, file upload) —
  never called; the legacy SOAP web services are superseded by the REST WS.

## CMS HyperNews archive (hypernews.cern.ch) — delivered

- **Discovery date**: 2026-10-26 (authenticated probes against the live site;
  routes cross-checked against the open-source viewer,
  github.com/henryiii/hypernewsviewer).
- **Status of the service**: CMS HyperNews was **discontinued at the end of
  2022 in favor of CMS Talk** (Discourse, cms-talk.web.cern.ch); 2022 was the
  overlap year. The site is a read-only archive; the viewer banner says
  "read-only archive" and the search date inputs are clamped to
  2000-01-01..2022-12-31. Every tool description carries this notice.
- **Surface**: server-rendered HTML only (Flask + Jinja), no JSON API — the
  adapter is a marked HTML-parsing fallback, like TWiki/DocDB.
- **Auth**: the whole site sits behind an oauth2-proxy (client
  `webframeworks-paas-hypernewsviewer`) fronting CERN SSO; anonymous → 302 to
  Keycloak. The shared SSO cookie session completes the handshake silently
  (verified live).
- **Endpoints** (all GET, all verified live):
  - `GET /HyperNews/CMS/search?query=Q[&start=D][&stop=D][&page=N]` —
    SQLite FTS5 full-text search, 50 results per page, FTS5 query syntax
    (phrases, AND/OR/NOT); simple queries run several seconds (~10 s).
    Results are `<div class="searchresult">` blocks: `/HyperNews/CMS/get//…`
    title anchor (note the double slash — `responses` begins with `/`),
    `YYYY-MM-DD HH:MM:SS` timestamp, `view-member.pl?responses=<user>`
    author anchor, snippet `<p>` with `<mark>`.
  - `GET /HyperNews/CMS/index` — all forums (~2900, ~640 KB page) sorted by
    last activity: `<b><a href=…get/NAME.html>TITLE</a></b> (YYYY-MM-DD)`.
  - `GET /HyperNews/CMS/cindex` — same forums grouped under
    `<div class="categoryheader">Category: NAME</div>`; used to resolve the
    `category` filter of `hypernews_list_forums`.
  - `GET /HyperNews/CMS/get/<forum>` — forum page (`<title>` + top-level
    threads in `<div class="listing"><ol>` `<li value=N>` items, author/date
    in `<em>(…)</em>`, reply counts as `N reply|replies`).
  - `GET /HyperNews/CMS/get/<forum>/<msg>[/<sub>…]` — message page: title
    `<h2>`, `Forum:`/`Date:`/`From:` header (`From` anchor carries the
    username both as query key and as `&lt;user&gt;`), body either wrapped in
    `<!--  start/end message body  -->` markers (plain-text content type) or
    as a bare `<div>` after the header `<dl>` (HTML content type), replies in
    the same listing structure. A missing message inside an existing forum
    returns HTTP 200 plain text "Unable to find message: …"; a missing forum
    or member makes the viewer raise HTTP 500.
  - `GET /HyperNews/CMS/view-member.pl?<username>` — member profile as flat
    `key: value<br>` lines (user_id, name, user_url, email, status, …). The
    route destructures the first query *key* as the username, so the
    parameter name itself carries it.
  - `GET /HyperNews/CMS/get/AUX/<path>` — attachments (verified live:
    717 KB PDF from TOP-16-001/78/2/1). Bodies reference them with
    site-relative (`get/AUX/…`) and absolute URLs, lowercase and uppercase
    `HREF` — extraction is case-insensitive and normalizes all forms.
- **Tools** (6): `hypernews_search`, `hypernews_list_forums`,
  `hypernews_get_forum`, `hypernews_get_message`, `hypernews_get_member`,
  `hypernews_download_file`.
- **Not implemented**: `view-members.pl` (member directory browsing, no
  lookup value over `hypernews_get_member`), `top.pl` (static welcome page),
  the `?dir=next-in-thread|nextResponse` redirects (the flags are reported on
  the message instead).

## CERN Admin e-guide (admin-eguide.web.cern.ch) — delivered

- **Platform**: Drupal 9 (meta-generator) behind CERN Keycloak
  (`webframeworks-drupal-admin-eguide` client); English (`/en`) and French
  (`/fr`) content. French aliases are also served without a language prefix
  (`/procedure/visas-...`).
- **Discovery**: no JSON API surface; `/sitemap.xml` is 404; the node-search
  endpoint is Drupal core search. Anonymous visitors only see the home page's
  public-procedures subset; `/{lang}/search/node`, `/{lang}/index-procedures`
  and all internal procedures 302 to `auth.cern.ch`. With an SSO session all
  return 200 (verified against the stored session, 2026-10-26).
- **Endpoints used** (all GET):
  - `/{lang}/search/node?keys=<q>&page=<n>` — 10 results/page; result list
    `<h2>Search results</h2><ol>` rows carry `h3[lang]` + absolute URL + `<p>`
    snippet; `?page=N` pager when there are more. Cross-language results.
  - `/{lang}/index-procedures` — A-Z view; interleaved `<h3>` letter headers
    and `views-row` anchors, optional `(FAQ)` companion link per row. 183
    procedure links when authenticated (probed). One row ("Life Insurance")
    precedes the first letter header.
  - `/{lang}/procedure/<slug>` — node page: `h1.page-header` title,
    `views-field-field-access` (public), `views-field-changed` (dd.mm.yyyy),
    `views-field-field-date-creation`, `views-field-field-processus-approbation`,
    `views-field views-field-body` (General), `views-field views-field-field-procedure`,
    `views-field-field-revisions` (`div.revision-table` rows:
    no/changes/approved-on), `views-field-field-ref-contacts` (contact teaser
    articles), sidebar blocks headed by `h1.text-center` (Reference documents,
    Useful documents, External links, Other procedures, Contacts) whose
    anchors are returned as `related_links`.
  - Files hosted under `/sites/default/files/...` download directly.
- **Mapping**: Keycloak login bounce (final URL on `auth.cern.ch` or
  `<title>Sign in to CERN`) → `AUTH_REQUIRED`; HTTP ≥ 400 → `UPSTREAM_ERROR`
  (retryable on 429/5xx via `status_code`); a fully unparseable procedure page
  → `UPSTREAM_ERROR` (not-found render).
- **Not implemented**: FAQ detail pages (`/en/faq/<slug>`; index/search
  surface the paths, pages parse as Drupal nodes), contact pages, the
  `/en/forms` listing, non-procedure content pages (`/en/content/...`),
  and the cernoverride theme's leaked PHP warning banner (ignored noise).

## CERN Service Portal / ServiceNow (cern.service-now.com) — delivered

- **Discovery date**: 2026-10-26 (authenticated probes against the live
  instance plus authenticated browser traffic capture of the SPA's own XHRs).
- **Platform**: ServiceNow Service Portal 1.x (Angular `sp-ng-app` shell
  composed via `/api/now/sp/page`, classic-era KB store). The instance is the
  CERN IT services portal ("CERN Service Portal", portal id
  `af91927edb821740ab34731b8c961927`).
- **Auth**: anonymous requests never authenticate — the SPA shell is served
  to everyone (200) without any SSO redirect, and the portal's own APIs then
  see a guest. A headless browser drive of the protected classic page
  `/service-portal/kb_search.do` triggers `saml_redirector.do` → silent CERN
  SAML handshake and drops the ServiceNow session cookies (`JSESSIONID`,
  `glide_session_store`, `glide_sso_id`, ...). Registered in
  `auth/browser.py:_COOKIE_ONLY_SITES`; the adapter probes the session before
  searching and re-warms once on 401/403 (JSONv2). `/api/now/v1/table/*` is
  role-gated: 401 "User is not authenticated" **even with a valid web
  session** — do not use.
- **Surfaces**:
  - `POST /api/now/sp/search` — the portal's own full-text search backend
    (captured from authenticated browser traffic; GET answers 405). Replayed
    with plain session cookies: 201 + JSON. Body: `{"query", "query_start_location",
    "count", "portal", "page", "include_facets", "include_suggestions"[, "source"]}`.
    Sources observed: `kb` (Knowledge articles), `actions` + `items`
    (service-catalog entries), `service_elements`, `outages`,
    `external_actions`, `information`. Result rows: `sys_id`, `number`/`name`,
    `label`, `short_description`, `text` snippet, `score`, `publishedUTC`,
    portal-relative `link`/`url` (absolutized by the adapter). Routed through
    `SafeHttpClient.request_json_post` (allowlisted to
    `cern.service-now.com`), the same read-only-POST precedent as the CMS
    Online form-POST.
  - **Limits (verified)**: no pagination and no totals —
    `pagination_supported=false`, `total_number_results=0` in every response;
    results capped at ~30/call across `result.results` +
    `result.additionalResults`. **Anonymous POST returns 201 with empty
    `results`** (silent guest search) — hence the pre-flight session probe
    via JSONv2.
  - `GET /kb_knowledge.do?JSONv2&sysparm_query=<key>=<value>` — legacy JSONv2
    read API; works with the portal session for KB articles and
    `kb_category` (label + `full_category` path). Anonymous → 401 (used as
    the session probe). **Limits (verified)**: `sysparm_fields` /
    `sysparm_limit` are ignored — a query returns every field of up to 50
    records (a text-index query inflated one response to 19.8 MB), so only
    exact-key lookups (`number=`/`sys_id=`) are size-safe. Canonical article
    portal URL comes from the record's `u_portal_page` (fallback
    `?id=kb_article&n=<number>`).
- **Tools** (3): `servicenow_search_service_portal`,
  `servicenow_search_kb` (portal search pinned to `sources=["kb"]`),
  `servicenow_get_kb_article` (number or sys_id; category resolved via a
  second JSONv2 lookup; body HTML stripped to plain text, 20k cap).
- **Not implemented**: service-catalog item detail (`sc_cat_item` JSONv2
  blanket read is accessible — verified — but search already links items and
  the requested surface was search + KB), portal facets
  (`include_facets=true` payload carries widget-level facet lists, no
  per-source totals), the `/api/now/ui/presence`-family noise endpoints the
  SPA posts to, and any user-account/`sys_user` surface (ACL-uncertain, no
  read-only value for the ask).


## CMS DQM GUI (cmsweb.cern.ch/dqm) — delivered

- **Discovery date**: 2026-10-27 (live grid-cert probes against `offline`/`relval`,
  plus the GUI source at github.com/cms-sw/dqmgui and the front-end
  configuration at github.com/dmwm/deployment).
- **Platform**: legacy DQM GUI JSON/HTTP API behind the CMS web front-end;
  instances map to backends by `frontend/app_dqm_ssl.conf` (`offline`,
  `online`, `relval`, `relval-test`, `offline-test`, `dev`, ...). The newer
  v1 routes (`/api/v1/...`, aiohttp app in the dqmgui sources) answer 404 on
  every deployed instance probed (`offline`, `offline-new`) — legacy API only.
- **Auth**: **X509 grid client certificate over TLS**. The front-end answers
  `HTTP 401` with an empty body to every unauthenticated path (no SSO
  redirect, no cookie login flow; TLS handshake requests a client cert).
  `AUTH_SPEC:aucookie;limited-proxy;cert;host` — the `cms-auth` cookie is
  only minted *after* a cert-authenticated request (`frontend/cmsauth.pm`,
  "for legacy reasons the cookie is set on X509 certificate accesses").
  Direct cookie requests with a valid CERN SSO session still answer 401
  (verified headlessly). Implementation: `Settings.grid_cert_hosts`
  (default `cmsweb.cern.ch`); `SafeHttpClient._client_for` lazily builds an
  SSL context from `grid_cert_file`/`grid_key_file` for those hosts; an
  injected test client always wins, so no real credential can touch a mock
  transport. The DQM tools never go `authenticated=True` (no SSO cookies are
  sent) and are not behind the SSO guard (auth model identical to DAS).
- **Surfaces** (all GET):
  - `data/json/samples?match=<regex>&run=<run>` — sample index.
    **`match` is a Python regex with `re.search` semantics** (not glob):
    `/ZeroBias/Run2026F* [...]` matches Run2026A because `F*` = zero-or-more
    F (verified live). No selectors at all returns every sample (~138 MB
    verified) — the adapter requires at least one of match/run. Same
    (run,dataset) pair can repeat per import version; deduped client-side.
    Items are `run:"401605"` (or `"401605:15"` with lumi), `dataset`,
    `importversion`.
  - `data/json/archive/<run>/<dataset>/<folder..>` — folder content:
    first entry is a `{"streamerinfo":""}` pseudo-entry, then
    `{"subdir": name}` (legacy) / `{"subdir": name, "me_count": n}` (new
    GUI), and ME entries `{obj, path, properties:{kind,type,lumisect,
    report:{alarm,error,warn,other}}, tag, qresults:[...], nentries,
    stats:{x,y,z}}`. The dataset must be bare (no leading slash; a leading
    slash is a 307 normalize redirect). `search=<substring>` filters ME
    names server-side. **Quirks:** an unknown (run,dataset) pair answers
    200 with *another* sample's content (e.g. run 999999 returned a live
    "00 Shift"/AlCaReco tree) — the adapter validates every pair against
    the samples index (regex-escaped exact lookup) before archive calls;
    an unknown folder answers 200 with only the streamerinfo pseudo-entry
    (mapped to a not-found error client-side).
  - `jsonfairy/archive/<run>/<dataset>/<me>` — JSON ME content for
    TH1-family (`{"hist":{type,title,stats,xaxis,values,bins:{integral,
    content}}}`); TH2/profiles and **nonexistent MEs alike** answer
    `{"hist": "unsupported type"}` (content-type text/html). The adapter
    resolves the ME through its parent folder listing first: a real
    not-found error when absent, ROOT-type/stats/qtest metadata either way,
    a `note` when the type is not serializable.
  - `plotfairy/archive/<run>/<dataset>/<me>?w=<px>&h=<px>` — rendered PNG.
    `w`/`h` are **effectively required**: unsized requests and nonexistent
    MEs alike return the *same* small placeholder image (verified: identical
    974-byte PNG) with HTTP 200. The adapter always sends explicit
    dimensions and pre-verifies the ME, so a missing ME is an error rather
    than a silent placeholder download.
- **Tools** (4): `dqm_list_samples`, `dqm_get_workspace`,
  `dqm_get_monitoring_element`, `dqm_download_plot`.
- **Not implemented**: `/api/v1/*` (not deployed), `plotfairy/overlay` /
  `jsrootfairy` (no identified consumer value beyond jsonfairy + PNG),
  online-shift/alarm-manager routes (session/workflow state, not read data),
  `POST /api/v1/register` (write-capable), `data/json/samples` without
  selectors (140 MB responses); lumi-granular ME lookups (routed through
  `run:lumi` item fields the JSON endpoints do not honor distinctly).
- **Live verification** (grid cert, `offline`): samples exact + regex
  lookups, archive root/folder/error shapes, jsonfairy TH1 content and
  unsupported-type shape, plotfairy sized PNG, placeholder plots, 401 without
  cert; `relval` samples 200.
