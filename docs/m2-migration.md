# M2 reference-tool migration matrix

Maps every tool of the four reference MCP servers to the consolidated CMcpS
namespace, including intentional removals.

## indico-mcp (InnovateInfinity87/indico-mcp, MIT, © 2024 CERN EPA-WP8)

| Reference tool            | CMcpS tool                     | Notes |
|---------------------------|--------------------------------|-------|
| `configure`               | removed                        | Replaced by env config `CMCPS_INDICO_BASE_URL` / `CMCPS_INDICO_API_TOKEN` |
| `get_user_info`           | `indico_get_user_info`         | Requires `CMCPS_INDICO_API_TOKEN` |
| `search_events`           | `indico_search_events`         | Legacy export API; `limit`/`offset` pagination, normalized `SearchResult` envelope |
| `search_categories`       | merged into `indico_search_events` | Same `/export/categ/{id}.json` endpoint; removed as duplicate |
| `search_events_by_term`   | `indico_search`                | `/search/api/search`, one call per content type |
| `get_event_details`       | `indico_get_event`             | Optional contributions/sessions |
| `get_event_contributions` | `indico_get_event_contributions` | Optional subcontributions |
| `get_files`               | `indico_get_event_files`       | Returns metadata + download URLs only, never inline content |
| `download_attachment`     | `indico_download_file`         | Bounded local download into CMcpS storage; returns path + size, never inline bytes/base64 |
| `search_rooms`            | removed                        | Stub in reference ("not yet implemented"); room booking is write-capable domain |
| `create_event`            | removed                        | Stub; would be a write |
| `book_room`               | removed                        | Stub; would be a write; needs legacy 40-char API key |
| `get_reservations`        | removed                        | Stub; room-booking domain out of read-only research scope |

Protected events/categories are invisible to the legacy export API unless the
request carries an Indico personal API token (`Authorization: Bearer`), set via
`CMCPS_INDICO_API_TOKEN`; without it they return `200` with `count: 0` and the
adapter answers "not found" plus configuration instructions.

## inspirehep-mcp (MohamedElashri/inspirehep-mcp, AGPL-3.0 — clean-room)

| Reference tool | CMcpS tool | Notes |
|----------------|------------|-------|
| `search_papers` | `inspire_search_papers` | Adds explicit `page` parameter |
| `get_paper_details` | `inspire_get_paper` | inspire_id / arxiv_id / doi |
| `get_author_papers` | `inspire_get_author_papers` | BAI resolution via /authors |
| `get_citations` | `inspire_get_citations` | `refersto:` / `citedby:recid:` query syntax |
| `search_by_collaboration` | `inspire_search_collaboration` | Alias map for common experiments |
| `get_paper_figures` | `inspire_get_paper_figures` | Links only |
| `get_references` | `inspire_get_references` | bibtex / latex-us / latex-eu / json |
| `get_bibtex` | `inspire_get_bibtex` | Identifier-type detection |

## cerngitlab-mcp (MohamedElashri/cerngitlab-mcp, AGPL-3.0 — clean-room)

| Reference tool | CMcpS tool | Notes |
|----------------|------------|-------|
| `test_connectivity` | removed | Duplicates `cern_health` |
| `search_projects` | `gitlab_search_projects` | |
| `get_project_info` | `gitlab_get_project` | Includes statistics + languages |
| `list_project_files` | `gitlab_list_files` | Adds `page` pagination |
| `get_file_content` | `gitlab_get_file` | Base64 decoded server-side; binaries return placeholder text |
| `get_project_readme` | `gitlab_get_readme` | Multi-candidate fetch, structured not-found result |
| `search_code` | `gitlab_search_code` | API only; no client-side grep fallback (advanced-search 400 surfaces as `UPSTREAM_ERROR`) |
| `search_lhcb_stack` | removed | LHCb-specific, depends on external lhcb-nightlies API; out of CMS scope |
| `search_issues` | `gitlab_search_issues` | Adds `page` parameter |
| `get_wiki_pages` | `gitlab_get_wiki` | List or detail by `page_slug` |
| `inspect_project` | removed | Heuristic composite analyzer; overlapping behavior and unbounded file fetching |
| `list_branches` | `gitlab_list_branches` | |
| `list_releases` | `gitlab_list_releases` | Adds `page` parameter |
| `get_release` | `gitlab_get_release` | |
| `list_tags` | `gitlab_list_tags` | Adds `page` parameter |

## cds-mcp (MohamedElashri/cds-mcp, license ambiguous: README claims MIT, no LICENSE file — clean-room)

| Reference tool | CMcpS tool | Notes |
|----------------|------------|-------|
| `search_cds_documents` | `cds_search` | Adds `page` parameter (`jrec`) |
| `get_cds_document_details` | `cds_get_record` | Accepts `123456` or `cds:123456` |
| `get_cds_document_files` | `cds_get_files` | Download links only |
| (new, no reference equivalent) | `cds_download_file` | Bounded local download; legacy iCMS (CMSFence) login placeholders detected and rejected with instructions |
| `get_cds_experiments` | removed | Static hard-coded list; filter values documented in tool descriptions |
| `get_cds_document_types` | removed | Static hard-coded list |
| `list_cds_collections` | removed | Static hard-coded list, unadvertised in the reference README |

Cross-cutting normalizations:

- All search/list tools use the `SearchResult` envelope
  (`source, items, total_count, page, size, has_more, query, sort, note`).
- Reference error strings/`{error: ...}` dicts became structured
  `ToolEnvelope` failures: `UPSTREAM_ERROR` (retryable on 429/5xx),
  `INVALID_INPUT`, `AUTH_REQUIRED`.
- File downloads return local paths under CMcpS download storage only;
  no inline base64, no placeholder text for binaries (GitLab's text decode
  for text files preserved because no local file is involved).
- Every tool is gated on `cern_auth_sign_in` unless
  `CMCPS_REQUIRE_SSO=false` is explicitly set for development.
