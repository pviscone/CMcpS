# CMcpS Delivery Plan

Build one read-only MCP server for CERN/CMS research and operations resources.

## Scope

Combine and normalize tools from these reference MCP implementations:

- https://github.com/InnovateInfinity87/indico-mcp
- https://github.com/MohamedElashri/inspirehep-mcp
- https://github.com/MohamedElashri/cerngitlab-mcp
- https://github.com/MohamedElashri/cds-mcp

Then add read-only search/detail/download tools for:

- https://cms-talk.web.cern.ch/ — Discourse
- https://cms-pub-talk.web.cern.ch — Discourse
- https://root-forum.cern.ch/ — Discourse
- https://geant4-forum.web.cern.ch/ — Discourse
- https://its.cern.ch/jira — Jira
- https://cms-results.web.cern.ch/cms-results/public-results/publications/index.html
- https://twiki.cern.ch
- https://cmsonline.cern.ch/
- https://cmsfence.cern.ch/ — GLANCE, replacing old iCMS/CADI
- https://cmssdt.cern.ch/SDT/
- https://cmsweb.cern.ch/das/
- https://xsecdb-xsdb-official.app.cern.ch
- https://cms-pdmv-prod.web.cern.ch/mcm/
- https://cms-search.web.cern.ch/
- https://gocms.cern.ch/

All tools are strictly read-only. Tools retrieve metadata, records, search results, HTML/text, PDFs, images, and plots. They must not create, update, delete, comment, vote, transition, upload, or change remote state.

## Delivery strategy

M1 ships authentication first and gates every tool on it. M2 merges the four existing MCP backends through one normalized tool contract and proves them against an authenticated session. M3 adds the remaining platforms one at a time, only after discovering and documenting the resource's public API, private API, or safe HTML fallback.

### M1 — Authentication and server foundation

Authoritative reference for CERN authentication (Keycloak/OIDC flows, SSO session behavior, token exchange used by the GLANCE-style Bearer adapters): https://auth.docs.cern.ch — official CERN Authentication and Authorization Service docs. Consult it when touching `cern_auth_sign_in`, cookie/token persistence, refresh, or RFC 8693 exchanges.

1. Review the reference MCP implementations for runtime, dependency, and license constraints before choosing the final implementation language and packaging.
2. Create the shared MCP server foundation:
   - `auth`: browser-backed CERN SSO session, cookie/token persistence, expiry checks, and refresh/relogin handling.
   - `config`: typed environment/settings model.
   - `http`: shared async HTTP client, retry/timeouts, bounded redirects, response limits, MIME handling, binary downloads, and redacted error logging.
   - `contracts`: common result/document/file/error models.
   - `registry`: tool registration and auth preflight.
   - `safety`: URL allowlists, read-only method enforcement, request-body rejection, domain limits, download limits, and credential redaction.
3. Expose an explicit first tool: `cern_auth_sign_in`.
   - Open the user's browser to CERN SSO.
   - Wait for successful login or a configurable timeout.
   - Capture the resulting cookies/tokens for whitelisted CERN domains.
   - Persist only what is necessary, outside source control, with restrictive permissions.
   - Return session status without exposing secrets.
4. Enforce SSO-first execution:
   - Every other tool starts with the authentication guard.
   - Without a valid session, tools return a structured `AUTH_REQUIRED` error instructing the caller to call `cern_auth_sign_in`.
   - MCP schema/tool descriptions make this prerequisite explicit because MCP does not provide a reliable universal call-order primitive.
   - Authentication bypass is optional, disabled by default, and intended only for offline/public-endpoint development and tests.
5. M1 acceptance gate:
   - Server starts and lists bootstrap/auth and health tools.
   - All non-auth tools are blocked before login.
   - A mocked login and a manually observed CERN SSO login create a reusable authenticated client.
   - Secrets never appear in logs, tool results, source, or tests.

### M2 — Consolidate existing MCP tools

1. Audit the four reference repositories:
   - Tool inventory and endpoint coverage.
   - Data models and pagination conventions.
   - Authentication methods.
   - Tests and fixtures.
   - Licenses and attribution requirements.
2. Reimplement rather than run four MCP servers side by side. One server should expose a coherent namespace and shared behavior:
   - Indico: event/search/detail/category/file retrieval tools as supported by the reference implementation.
   - INSPIRE-HEP: literature/search/detail/reference/citation/author tools as supported by the reference implementation.
   - CERN GitLab: project/group/repository/file/search tools as supported by the reference implementation.
   - CDS: record/search/detail/file tools as supported by the reference implementation.
3. Normalize tool contracts:
   - Common pagination arguments (`limit`, cursor or offset as appropriate).
   - Common date/field/sort filters where the backend supports them.
   - Canonical `resource_uri`, `title`, `summary`, `authors`, `created_at`, `updated_at`, and `source` fields when available.
   - Predictable `items`, `next_cursor`, and `total_count` search envelopes.
   - Download tools return metadata plus local path/base64 according to a single policy, never mixed formats.
4. Route all requests through the SSO-aware HTTP layer:
   - Keep service-specific API-token support where the reference implementation requires it.
   - Inject SSO cookies only for configured CERN domains.
   - Never send credentials or tokens to unrelated origins.
5. Preserve useful behavior, not arbitrary implementation:
   - Delete duplicate, deprecated, write-capable, or untestable tools instead of carrying them forward.
   - Keep compatibility aliases only if the audit finds a compelling existing-user dependency.
6. M2 acceptance gate:
   - The consolidated server lists every retained Indico/INSPIRE/GitLab/CDS tool under the new namespace.
   - Each retained tool has schema, parser, read-only-method, and contract tests.
   - Public INSPIRE/CDS behavior can be tested with `BLOCKSSO` disabled in development automation; authenticated CERN tools pass after the M1 login flow.
   - A regression matrix maps every retained original tool to its new tool name and captures any intentional removals.

### M3 — Add new CERN/CMS resource tools one by one

For every platform, use the same adapter lifecycle before implementation:

1. Discover:
   - Check for official/public APIs, OpenAPI definitions, RSS/Atom, structured data, documented Jira REST endpoints, Django REST endpoints, or GraphQL endpoints.
   - Inspect authenticated browser/network behavior only when no documented endpoint exists.
   - Record endpoints, auth requirements, pagination, rate limits, query parameters, response fields, and download URLs in the adapter documentation.
2. Define the smallest useful read-only surface:
   - Search/list.
   - Detail/view.
   - Download/export where supported.
   - Domain-specific convenience filters only when they map directly to backend capabilities.
3. Implement:
   - One platform adapter at a time.
   - Reuse shared search/detail/download contracts.
   - Prefer APIs; use HTML parsing only as a clearly marked fallback.
   - Restrict requests to GET/HEAD and known-safe OPTIONS only if required by the platform.
4. Test:
   - Unit-test URL construction and response normalization with captured fixtures.
   - Add contract tests for pagination, sparse responses, backend errors, auth expiry, and binary downloads.
   - Run live checks only behind explicit environment markers; never make private CERN data mandatory for default tests.
5. Document:
   - Tool descriptions, required arguments, authentication needs, provenance, endpoint type, and known limits.

Roll out M3 in this order, while allowing re-ordering if discovery proves a simpler path:

1. Generic Discourse adapter: `cms-talk`, `cms-pub-talk`, `root-forum`, and `geant4-forum` share Discourse's search/topics/posts/users/files API surface, with each site as an adapter instance.
2. CMS public results: publications and likely conference/plot resources through static/structured public pages.
3. Twiki: page/raw/attachment retrieval; preserve legacy URL patterns. Full-text search is via cmssearch (indices `cms-twiki`/`cms-public-twiki`).
4. CMS Online: detector/status/operations search after endpoint discovery.
5. GLANCE/CMSFence: metadata/search/detail/export after authenticated discovery.
6. CMS SDT: build/integration/repository status queries.
7. CMS DAS: dataset/block/file/site/summary queries.
8. XSDB: cross-section/search/detail retrieval.
9. McM: request/prepid/search/detail retrieval; read-only only.
10. CMS Search: unified internal search wrapper.
11. goCMS: MkDocs/internal documentation search and page retrieval.

M3 acceptance gate is per adapter, not a single giant gate. An adapter is complete only when its search and at least one detail/download path work from captured fixtures and, where feasible, from an authenticated smoke run.

## Architecture

Target a single server package with these internal boundaries:

```text
src/…
├── auth/        # browser SSO, session store, token/cookie policies
├── config/      # typed settings, endpoint and domain allowlists
├── contracts/   # requests, tools, results, files, errors
├── http/        # async clients, downloads, retries, limits, redaction
├── registry/    # MCP tool registration and guard
├── safety/      # read-only and domain enforcement
├── adapters/    # one module per backend or platform family
└── tests/       # unit, contract, fixture, and marked live tests
```

Implementation rules:

- Use one shared async HTTP/session implementation rather than one client per adapter.
- Do not let adapters construct arbitrary raw requests outside the safety layer.
- Decode and validate responses at adapter boundaries; pass structured output to MCP tools.
- Keep tools deterministic: no hidden background jobs beyond SSO browser waiting.
- Avoid copying large reference implementation code unless licenses and behavior justify it; normalize concepts and cite attribution.
- Keep backwards compatibility only through explicit, documented aliases.
- Treat all CERN content as potentially sensitive: log URLs with secrets redacted, never log cookies/tokens/authorization headers, and bound response/download sizes.

## Testing strategy

- Unit tests: parsers, URL builders, pagination, error normalization, auth guard, MIME/download handling.
- Fixture/contract tests: captured representative responses for every adapter; no brittle full-response snapshots unless they catch regressions.
- Read-only safety tests: every adapter proves it issues no mutating method/body and each MCP tool inherits the auth guard.
- Integration tests: server startup, tool listing, schema generation, auth-required behavior, and representative tool calls with mocked transports.
- Live optional tests:
  - `CMCPS_LIVE_PUBLIC=1`
  - `CMCPS_LIVE_CERN=1` after interactive SSO
  - no live tests in the default suite.
- Manual verification after each milestone:
  - start the real server;
  - list tools;
  - authenticate with a browser;
  - exercise one search and one detail/download path per newly completed milestone.

## Milestone completion criteria

A milestone is done only when all are true:

1. The implementation compiles/passes static checks and tests.
2. The server runs and exposes the planned tools.
3. Auth and read-only safety are proven by tests plus a manual smoke path.
4. Removed or renamed reference tools are listed in the migration notes.
5. Every new adapter documents its API discovery and endpoint type.
6. Documentation explains setup, optional tokens, SSO flow, testing flags, and data-handling boundaries.
7. No scaffolds, dead aliases, write-capable remnants, fake network success, credential prints, or unfinished TODOs remain.

## Open risks and controls

- Private CMS services often expose undocumented UI-first APIs. Control: perform endpoint discovery before writing any adapter; prefer official API results over HTML extraction.
- Some services may require service-specific tokens in addition to SSO cookies. Control: support explicit, namespaced tokens; never assume one cookie covers every backend.
- Slow or rate-limited services need consistent behavior. Control: per-domain timeout, retry, concurrency, and size policies in the shared HTTP layer.
- Large downloads can exhaust MCP transport/context. Control: return metadata plus a bounded local file path by default; return inline bytes only for small files with an explicit size cap.
- MCP cannot guarantee clients call auth first. Control: authenticate each call in the server and return actionable `AUTH_REQUIRED` rather than trusting client call order.

## Current status — M2 complete, M3 mostly delivered

M1 and M2 are implemented and green. M1 (auth foundation) is unchanged in behavior; M2 added the consolidated Indico, INSPIRE-HEP, CERN GitLab, and CDS read-only tools on that foundation.

### M3 delivered (9 platforms, 36 tools, `docs/m3-adapters.md` for all discovery notes)

- `discourse_*` (7): one generic Discourse adapter with `site` enum (`cms-talk`, `cms-pub-talk`, `root-forum`, `geant4-forum`); search, latest/top/category topics, topic (paged rendered posts, plain text + uploads), post (raw when public), user, bounded download. Live-verified on root-forum (public); CMS forums need SSO.
- `cmsresults_*` (4): static-HTML fallback for cms-results (search backend was persistently 5xx during discovery); groups, per-group papers, paper detail (abstract/links/figures), download. Live-verified.
- `twiki_*` (3): page content via the `cover=print` render as plain text with footer-metadata parsing (revision/author/date/parent/attachments; CERN TWiki does not serve `%META` raw source), subweb (`Web/Subweb`) support, public-web topic list, attachment download. TWiki full-text search goes through cmssearch (indices `cms-twiki`/`cms-public-twiki`) — the CGI search was removed (slow/flaky, no snippets). Live-verified (`CMS/Internal` page, 2026-10).
- `cmsonline_*` (5): OMSS API (`cmsoms.cern.ch/agg/api/v1`) — portal itself is a JS WebCenter with no REST surface; runs/fills/lumisections/version. 401 anonymous; fixture-tested only, live pending SSO.
- `jira_*` (3): its.cern.ch REST v2 — JQL search (anonymous OK for public projects), issue detail, project. Sign-in captures the Jira session via the silent OIDC handshake; adapter re-warms on 401/403 and flags public-only scope in `SearchResult.note`.
- `mcm_*` (4): McM public REST index (no SSO); get request (empty-`results` → not-found), status+approval, output datasets, exact-dataset produces lookup. All live-verified.
- `das_*` (2): DAS QL search + exact dataset info; SSO-fronted (401 anonymous), fixture-tested.
- `gocms_*` (3): goCMS is the CMS **shortlink service** (FastAPI, public OpenAPI) — not the assumed docs wiki. Links search/list/detail + tags. Live-verified.
- `cmssdt_list_releases` (1): ReleasesXML feed (public; parsed with `defusedxml`). Live-verified. IB dashboards are SSO-only.
- `xsdb_*` (2): search + linked tables; every route OIDC-gated (existence verified), fixture-tested, live pending SSO.

### Post-M3 extension (authenticated discovery completed)

After a real SSO session became available, the two deferred platforms were delivered plus the SDT dashboard surface was completed:

- `glance_*` (11): ALCM/CADI and Membership SPAs at cmsfence.cern.ch use per-app Keycloak **Bearer** tokens (audiences `cms-alcm-api-prod` / `cms-membership-api-prod`) issued via RFC 8693 token exchange; tokens are minted lazily on first use (`BEARER_SERVICES` + `capture_bearer_service`), refresh goes through refresh→re-exchange (`BEARER_SERVICE_EXCHANGE_AUDIENCES`) because a plain refresh loses the API audience. Cookie-only calls answer 401. Env fallbacks `CMCPS_GLANCE_ALCM_TOKEN`/`CMCPS_GLANCE_MEMBERSHIP_TOKEN`. `glance_search_notes` needs `CMCPS_MAX_RESPONSE_BYTES` > ~16 MB (single ~15 MB upstream index). Incubator SPA not implemented (nothing useful read-only).
- `cmssearch_*` (4): FastAPI "CERN Batch Docs Search" (public OpenAPI, Keycloak-proxied) — search, document, sources, index stats. Cookie SSO suffices.
- `cmssdt_*` extended (+7, now 8): IB queues, per-arch latest-IB summary, IB comparison details, CMSSW labels, open PRs per release family, RelVal archs, and a pure-derivation build-log URL tool.
- `docdb_*` (4): CMS DocDB (cms-docdb.cern.ch) is Perl-CGI HTML-only; GET search (Keycloak blocks the POST form), ShowDocument metadata parse, see-recent list, RetrieveFile downloads. cms-docdb serves a CERN Grid CA host certificate absent from certifi, so the shared client now uses the system CA store. Live-verified (LAGG document 14372: search, get, PDF download).
- `hypernews_*` (6): CMS HyperNews archive (hypernews.cern.ch) — discontinued end of 2022 in favor of CMS Talk (2022 overlap year; stated in every tool description). HyperNewsViewer is HTML-only behind OAuth-proxy SSO; FTS5 search (50/page, dates clamped to 2000-2022), forums by activity+category, forum threads, message detail (header/body/replies, "Unable to find message" 200 → 404-class error, missing forum/member → 500), member profiles (username passed as query *key*), AUX attachment extraction (case-insensitive, relative+absolute) and bounded downloads. Live-verified end to end (search, forums, message, member, 717 KB attachment download).
- `eguide_*` (4): CERN Admin e-guide (Drupal, Keycloak-gated search/index nodes); search, A-Z procedure index, procedure detail with related links, bounded downloads. Live-verified.
- `mattermost_*` (9): CERN Mattermost (mattermost.web.cern.ch, stock API v4); me/user/teams/channels/channel detail, post search (read-only JSON POST, allowlisted + warm-retried; cookie-auth POSTs need `X-Requested-With: XMLHttpRequest` — verified; results clamp to 100 hits), channel history with before/after cursors, thread reads, bounded file downloads. Verified fork quirks handled: teams/channels endpoints ignore `page`/`per_page` (tools expose no paging; channels returns the account-wide list including DMs — filtered to the requested team client-side). Auth is the `MMAUTHTOKEN` cookie from the `/oauth/gitlab/login` handshake (mm-auth.web.cern.ch brokers to auth.cern.ch — silently warmed from the stored identity as a `_COOKIE_ONLY_SITES` entry); `CMCPS_MATTERMOST_TOKEN` PAT fallback. Anonymous API → JSON 401; 403 stays a permission error. Live-verified end to end (get_me, get_user, teams, channels, channel detail, channel history, thread, search, 218 KB attachment download).
- `servicenow_*` (3): CERN Service Portal (cern.service-now.com ServiceNow SP 1.x). The SPA never forces SSO for HTTP clients — anonymous hits get a guest context (silent empty search results, verified) — so the ServiceNow session is planted by a headless SAML drive (`auth/browser.py:_COOKIE_ONLY_SITES`, `kb_search.do` → `saml_redirector.do`) and probed before searching (`<table>.do?JSONv2` sentinel lookup). Search is `POST /api/now/sp/search` (captured from authenticated SPA traffic; GET 405) — routed through the new `SafeHttpClient.request_json_post` allowlisted to this host (read-only POST precedent alongside the CMS Online form-POST). Results carry grouped hits (kb/actions/items/service_elements/outages/external_actions/information), no paging/totals (backend limitation, `pagination_supported=false`). Article reads use the legacy `kb_knowledge.do?JSONv2` exact-key lookups (`sysparm_fields`/`sysparm_limit` NOT honored — full-field, up-to-50-record responses; `/api/now/v1/table/*` 401s even in-session). Live-verified (KB search → article detail with category resolution → full portal search).
- uvx: `uvx --from <path-or-git-url> cmcps` verified (106 tools over stdio); plain `uvx cmcps` awaits a PyPI publish.

149 tools total (5 bootstrap + 144 adapter); full suite/lint/build green; SDT/GLANCE/CMS-search/DocDB/HyperNews/ServiceNow/Mattermost live-verified with an SSO session (GLANCE through the refresh→exchange chain).

### M3 verification

- `261 passed`; ruff lint + format clean; `uv build` succeeds.
- Stdio smoke: 69 tools listed; all M3 tools auth-gated in `ADAPTER_CALLS`.
- Live public smokes (`CMCPS_REQUIRE_SSO=false`): Discourse (root-forum search→topic), cms-results (groups/papers/detail), Twiki (anonymous topic list + rendered page), McM (request/status/output/produces), SDT (release filters), goCMS (links/tags/detail).
- New dependency: `defusedxml` (SDT XML parsing).

M2 sections below are unchanged.

### M2 delivered

- 31 adapter tools under four namespaces, all behind the SSO guard:
  - `indico_*` (7): user info, category event search, full-text search, event detail, contributions, event files, bounded downloads.
  - `inspire_*` (8): paper search/detail, author papers, citations, collaborations, figures, references, BibTeX.
  - `gitlab_*` (12): project search/info, tree/file/readme, code and issue search, wiki, branches, releases, tags. Optional `CMCPS_GITLAB_TOKEN` (PAT, `PRIVATE-TOKEN` header).
  - `cds_*` (4): search, record detail, file links, bounded downloads with iCMS stub detection. Legacy `of=recjson` endpoint; Anubis HTML detected with browser-sign-in guidance.
- Shared plumbing:
  - `SearchResult` envelope (`cmcps/contracts/results.py`) for every search/list tool.
  - `UpstreamError` contract (retryable on 429/5xx) plus `cmcps/adapters/common.py` JSON decoding.
  - `SafeHttpClient.request(..., service_headers=...)` injects operator-configured tokens from `Settings` only; tool arguments still cannot supply credential headers.
  - `SafeHttpClient.download` follows bounded redirects with per-hop cookie re-scoping.
  - Tool registration split by backend into `src/cmcps/tools/{indico,inspire,gitlab,cds}.py` via `register_adapter_tools` in `src/cmcps/tools/__init__.py`.
- Docs: `README.md` tool inventory + config table, `.env.example`, migration matrix with intentional removals at `docs/m2-migration.md`.

### M2 verification

- `144 passed` (adapter fixture tests, auth-gate parametrization over all 30 tools, server/stdio integration).
- Ruff lint and format checks pass; `uv build` succeeds.
- Manual stdio smoke: 33 tools listed; unauthenticated `cds_get_record`/`gitlab_get_project` return `AUTH_REQUIRED`.
- Live public smoke with `CMCPS_REQUIRE_SSO=false`: real INSPIRE search returned parsed papers and a working `inspire_get_bibtex` round trip.

### M2 known boundaries

- Indico/CDS/GitLab authenticated behavior was only fixture-tested; a live authenticated smoke needs the user's real CERN SSO login plus optional Indico/GitLab tokens (same open item as M1).
- `gitlab_search_code` intentionally has no client-side grep fallback; without GitLab advanced search the backend 400 surfaces as `UPSTREAM_ERROR`.
- CDS has no reliable total hit count (`total_count=None`, `has_more` derived from page size).

### Important implementation files (M2)

- `src/cmcps/adapters/{indico,inspire,gitlab,cds,common}.py` — backend logic.
- `src/cmcps/tools/{indico,inspire,gitlab,cds,common}.py` + `__init__.py` — MCP registration and guard wiring.
- `tests/test_adapter_{indico,inspire,gitlab,cds}.py` — fixture/contract tests with `httpx.MockTransport`.
- `tests/test_server.py` — `ADAPTER_CALLS` parametrized auth-gate proofs; `ALL_TOOLS` inventory assertion.

### M3 starting instructions

- Follow the M3 adapter lifecycle (discover → define smallest read-only surface → implement → fixture-test → document) for the platforms listed above, starting with the generic Discourse adapter (`cms-talk`, `cms-pub-talk`, `root-forum`, `geant4-forum`).
- New adapters: add `src/cmcps/adapters/<name>.py`, `src/cmcps/tools/<name>.py`, `tests/test_adapter_<name>.py`; register in `src/cmcps/tools/__init__.py`; extend `ADAPTER_CALLS`/`ALL_TOOLS` in `tests/test_server.py`; document in `README.md`.
- New non-`*.cern.ch` hosts must be added to `DEFAULT_ALLOWED_HOSTS` in `src/cmcps/config/settings.py`.

### M1 delivered

- Python MCP SDK 2.x stdio server.
- Registered tools:
  - `cern_auth_sign_in`
  - `cern_auth_reauthenticate`
  - `cern_auth_status`
  - `cern_health`
  - `cern_fetch_page` — headless SSO-protected page render through the stored session (works for cookie-based sites and Keycloak-PKCE SPAs whose token exchange completes silently); `wait_for` polls for a case-insensitive substring, `auth_divert` flags a stale session that landed on the SSO login.
- Visible Playwright-backed CERN SSO flow with system Chromium/Firefox detection.
- Lazy Bearer mint: `BEARER_SERVICES` exchanges are captured headlessly on first adapter use (`capture_bearer_service` via `service_bearer`), and cookie-only site sessions self-warm centrally: `SafeHttpClient.request` retries once after a mapped-site 401/403 via `warm_sites` (`cookie_site_url_for_host` reverse map). The visible browser closes right after login.
- Server-side auth guard with structured `AUTH_REQUIRED` results.
- Minimized credential persistence:
  - only allowlisted cookies are stored;
  - localStorage/origins are dropped;
  - state directory uses `0700`;
  - session file uses `0600`;
  - identity cookies must be secure, `httpOnly`, exact-IdP-scoped, and within bounded age.
- Read-only HTTP layer:
  - `GET`/`HEAD` only;
  - HTTPS and host allowlist enforcement;
  - RFC 6265 cookie domain/path matching;
  - per-redirect auth re-scoping;
  - bounded redirects, response sizes, and downloads;
  - downloads constrained to CMcpS state storage.
- Credential-redaction and output hygiene:
  - cookie/authorization headers rejected from adapter callers;
  - CERN tickets, SAML artifacts, JWTs, assertions, tokens, sessions, keys, and signatures redacted from URLs;
  - tool results expose only the credential basename, never the absolute path;
  - no implicit `.env` loading from the MCP client's working directory.
- Documentation and packaging:
  - `README.md`
  - `.env.example`
  - `.gitignore`
  - `pyproject.toml`
  - `uv.lock`

### M1 verification

- `50 passed`
- Ruff lint and format checks pass.
- Wheel and source distribution build successfully directly with `uv build`.
- Real MCP stdio startup, tool discovery, auth status, and `AUTH_REQUIRED` health behavior were exercised.
- Visible browser launch was smoke-tested; without completing personal CERN credentials, it returned the expected retryable `AUTH_TIMEOUT`.
- Independent code review marked M1 correct.
- Independent security review marked all 13 findings resolved; the final residual absolute-path error disclosure was then fixed and regression-tested.

### Important implementation files

- `src/cmcps/app.py` — MCP tool registration and server startup.
- `src/cmcps/auth/browser.py` — visible CERN SSO collection.
- `src/cmcps/auth/service.py` — auth lifecycle and server-side guard.
- `src/cmcps/auth/store.py` — minimized/atomic credential persistence and cookie scoping.
- `src/cmcps/config/settings.py` — environment-backed typed settings.
- `src/cmcps/net/safety.py` — URL, method, header, and redaction policy.
- `src/cmcps/net/client.py` — policy-bound async HTTP/download layer.
- `src/cmcps/contracts/` — stable errors and tool result envelopes.
- `tests/` — M1 unit, contract, security, and real stdio integration tests.

### Reference-repository audit constraints for M2

- Reimplement in Python; do not import four servers side by side.
- Do not copy code from the AGPL-3.0 `inspirehep-mcp` or `cerngitlab-mcp` repositories unless the project license is deliberately changed. The current M1 implementation is clean-room.
- MIT reference implementations may be consulted for endpoint/tool behavior, with attribution if implementation material is reused.
- `cds-mcp`'s OIDC client-credentials path is known not to authenticate CDS user access and is blocked by Anubis.
- Indico uses an optional API token; keep a service-specific token path when M2 implements it.
- CERN GitLab can support public/PAT-authenticated API access; keep namespaced env credentials separate from SSO cookies.
- INSPIRE-HEP is public, but M1's default server-side SSO guard still applies unless an explicit development bypass is enabled.

### M2 starting instructions

1. Audit the four reference tool inventories and map retained tools to the new namespace.
2. Reuse `AuthService`, `SafeHttpClient`, `ToolEnvelope`, settings, and safety contracts; do not create adapter-specific raw HTTP or credential handling.
3. Implement one backend at a time with URL/parser tests using recorded fixtures.
4. Add server tests proving every new tool is auth-gated and read-only.
5. Keep public-service development behind explicit `CMCPS_REQUIRE_SSO=false`; default behavior must remain SSO-first.
6. Update `README.md` tool inventory and the retained/removed reference-tool migration matrix as each backend lands.
7. The only M1 gap requiring the user's real credentials is a completed live CERN SSO login; browser launch and timeout paths were verified, but no personal credential login was performed in this session.

### Quick start for a new M2 session

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run cmcps
```
