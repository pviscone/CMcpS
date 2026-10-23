"""Read-only and credential safety policy tests."""

from __future__ import annotations

import pytest

from cmcps.contracts.errors import PolicyViolationError
from cmcps.net.safety import (
    host_matches,
    redact_url,
    validate_headers,
    validate_method,
    validate_url,
)


def test_host_matching_is_exact_or_subdomain_only() -> None:
    patterns = ("*.cern.ch", "inspirehep.net")
    assert host_matches("service.cern.ch", patterns)
    assert host_matches("cern.ch", patterns)
    assert host_matches("inspirehep.net", patterns)
    assert not host_matches("notcern.ch", patterns)
    assert not host_matches("service.cern.ch.evil.example", patterns)


def test_validate_url_accepts_allowlisted_https_subdomain() -> None:
    url = validate_url(
        "HTTPS://SERVICE.CERN.CH/path?x=1#fragment",
        allowed_hosts=("*.cern.ch",),
    )
    assert url == "https://service.cern.ch/path?x=1"


@pytest.mark.parametrize(
    "url",
    [
        "http://service.cern.ch/path",
        "https://evil.example/path",
        "https://user:secret@service.cern.ch/path",
    ],
)
def test_validate_url_rejects_unsafe_destinations(url: str) -> None:
    with pytest.raises(PolicyViolationError):
        validate_url(url, allowed_hosts=("*.cern.ch",))


def test_validate_method_rejects_mutation_and_body() -> None:
    with pytest.raises(PolicyViolationError):
        validate_method("POST")
    with pytest.raises(PolicyViolationError):
        validate_method("GET", body={"x": 1})


def test_caller_credentials_must_use_auth_service() -> None:
    with pytest.raises(PolicyViolationError):
        validate_headers({"Cookie": "name=value"})
    with pytest.raises(PolicyViolationError):
        validate_headers({"Authorization": "Bearer token"})
    assert validate_headers({"Accept": "application/json"}) == {"Accept": "application/json"}


def test_redact_url_hides_likely_secret_query_values() -> None:
    url = redact_url(
        "https://user:secret@service.cern.ch/path?token=abc&access_token=def&ordinary=ok"
    )
    assert url == (
        "https://service.cern.ch/path?token=%3Credacted%3E&access_token=%3Credacted%3E&ordinary=ok"
    )
    assert "abc" not in url
    assert "def" not in url
    assert "user:secret" not in url


def test_redact_url_hides_cern_sso_and_saml_artifacts() -> None:
    url = redact_url(
        "https://service.cern.ch/callback?"
        "ticket=ST-123-secret&SAMLResponse=xml-secret&jwt=jwt-secret"
    )
    assert "ST-123-secret" not in url
    assert "xml-secret" not in url
    assert "jwt-secret" not in url
    assert "ticket=%3Credacted%3E" in url
    assert "SAMLResponse=%3Credacted%3E" in url
    assert "jwt=%3Credacted%3E" in url
