"""Read-only adapter for the CERN Phonebook (phonebook.cern.ch).

directory.web.cern.ch is a Drupal portal whose search tabs delegate to other
services (CDS, Service Portal, Indico, CERN Map — all covered by their own
adapters); its unique surface is the **CERN Phonebook** of people and
services, served by a React SPA at phonebook.cern.ch.

Endpoint type: private JSON API discovered from the SPA bundle
(``assets/index-*.js``):

- ``GET /api/search?query=<q>&page=<n>`` — full-text search over people and
  services; Spring-style page (``content``, 0-based ``number``, ``size`` 10
  fixed, ``totalElements``, ``totalPages``, ``first``/``last``). Each content
  row is a PERSON (firstName, lastName, email, organicUnit, building/office
  ``location``, phones as ``{displayNumber, fullNumber}``, ``postBox``,
  ``preferredLanguage``) or a SERVICE (``name``, ``email``, ``snowId``).
- ``GET /api/search/lastUpdate`` — directory database freshness
  (``{"lastUpdate": "<iso>"}``).

Discovery notes: every ``/api/*`` route answers anonymous requests with
``401 WWW-Authenticate: Bearer resource_metadata=...``; the SPA authenticates
with the token of its own Keycloak public client ``phonebook`` (realm
``cern``), attached via an interceptor. The token is minted headlessly from
the stored SSO session on first use (``BEARER_SERVICES``) and the API accepts
the client-scoped token directly — no RFC 8693 audience exchange (verified
against the live API: ``tls_client_certificate_bound_access_tokens`` in the
protected-resource metadata is *not* enforced server-side for plain Bearer
HTTPS calls). All frontend routes (``/``, ``/search``) return the SPA shell;
the API is JSON-only.
"""

from __future__ import annotations

from typing import Any

from cmcps.adapters.common import json_body
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.contracts.results import SearchResult
from cmcps.net.client import SafeHttpClient

SOURCE = "phonebook"

_QUERY_MAX = 200
_PAGE_MIN = 1

# ``last`` is absent on edge cases in older Spring releases; treat a missing
# flag as "no more pages" (defensive default, never silently drops totals).
_NO_MORE_PAGES = True


async def _service_headers(client: SafeHttpClient, settings: Settings) -> dict[str, str]:
    token = await client.bearer_for("phonebook")
    if token is None:
        token = settings.phonebook_token
    if not token:
        raise InvalidInputError(
            "phonebook tools require a Keycloak Bearer token",
            instructions=(
                f"The {settings.phonebook_app_url} Bearer token is minted "
                "automatically from the stored SSO session on first use — sign "
                "in with cern_auth_sign_in first. Alternatively set "
                "CMCPS_PHONEBOOK_TOKEN manually."
            ),
        )
    return {"Authorization": f"Bearer {token}"}


def _base(settings: Settings) -> str:
    return settings.phonebook_app_url.rstrip("/")


def _validate_search_args(query: str, page: int) -> tuple[str, int]:
    cleaned = " ".join(query.split())[:_QUERY_MAX]
    if not cleaned:
        raise InvalidInputError(
            "empty phonebook query",
            instructions="Pass a name, surname, email fragment, or service name.",
        )
    if page < _PAGE_MIN:
        raise InvalidInputError(
            f"invalid page {page}",
            instructions="Pages are 1-based; pass page >= 1.",
        )
    return cleaned, page


async def search(
    client: SafeHttpClient,
    settings: Settings,
    *,
    query: str,
    page: int = 1,
) -> SearchResult:
    """Full-text search over CERN people and services (1-based pages of 10)."""
    cleaned, page = _validate_search_args(query, page)
    response = await client.request(
        "GET",
        f"{_base(settings)}/api/search",
        params={"query": cleaned, "page": page - 1},
        service_headers=await _service_headers(client, settings),
    )
    body = json_body(response, source=SOURCE)
    if not isinstance(body, dict) or not isinstance(body.get("content"), list):
        # Backend contract is a Spring page; anything else means a changed API.
        raise UpstreamError(
            "phonebook returned an unexpected search payload",
            instructions="Report the response shape; the backend contract may have changed.",
        )
    items = [item for item in body["content"] if isinstance(item, dict)]
    number = body.get("number", page - 1)
    is_last = body.get("last", _NO_MORE_PAGES)
    total = body.get("totalElements")
    size = body.get("size")
    return SearchResult(
        source=SOURCE,
        items=items,
        total_count=total if isinstance(total, int) else None,
        page=(number + 1) if isinstance(number, int) else page,
        size=size if isinstance(size, int) else len(items),
        has_more=not is_last,
        query=cleaned,
    )


async def last_update(
    client: SafeHttpClient,
    settings: Settings,
) -> dict[str, Any]:
    """Directory database last-update timestamp (``{"lastUpdate": ...}``)."""
    response = await client.request(
        "GET",
        f"{_base(settings)}/api/search/lastUpdate",
        service_headers=await _service_headers(client, settings),
    )
    body: Any = json_body(response, source=SOURCE)
    if not isinstance(body, dict) or not isinstance(body.get("lastUpdate"), str):
        raise UpstreamError(
            "phonebook returned an unexpected lastUpdate payload",
            instructions="Report the response shape; the backend contract may have changed.",
        )
    return {"source": SOURCE, "last_update": body["lastUpdate"]}
