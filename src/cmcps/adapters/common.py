"""Shared helpers for M2 adapters backed by ``SafeHttpClient``.

Adapters receive a ``SafeHttpClient`` (already auth-guarded and policy-bound)
plus ``Settings`` and return plain JSON-serializable structures. They never
construct raw requests outside the safety layer.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any, ClassVar

from cmcps.contracts.errors import UpstreamError
from cmcps.net.client import SafeResponse

_MIN_ERROR_STATUS = 400
_AUTH_STATUSES = frozenset({401, 403})
_ERROR_DETAIL_LIMIT = 500


def _error_detail(content: bytes) -> str:
    """Extract a bounded, single-line diagnostic from an error response body."""
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        # Jira-style: {"errorMessages": [...], "errors": {field: msg}}
        messages = body.get("errorMessages")
        errors = body.get("errors")
        detail = "; ".join(str(m) for m in messages) if isinstance(messages, list) else ""
        if isinstance(errors, dict) and errors:
            detail = "; ".join(filter(None, [detail, str(errors)]))
        if detail:
            return " ".join(detail.split())[:_ERROR_DETAIL_LIMIT]
    return " ".join(text.split())[:_ERROR_DETAIL_LIMIT]


def json_body(response: SafeResponse, *, source: str) -> Any:
    """Decode a JSON backend response or raise a structured upstream error."""
    if response.status_code >= _MIN_ERROR_STATUS:
        message = f"{source} request failed with status {response.status_code} for {response.url}"
        if detail := _error_detail(response.content):
            message = f"{message}: {detail}"
        if response.status_code in _AUTH_STATUSES:
            instructions = (
                "The session is missing or expired for this site; "
                "re-sign in with cern_auth_reauthenticate and retry."
            )
        else:
            instructions = "Check the request arguments and try again."
        raise UpstreamError(message, status_code=response.status_code, instructions=instructions)
    content_type = response.headers.get("content-type", "")
    try:
        return json.loads(response.content.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        snippet = " ".join(
            response.content[:_ERROR_DETAIL_LIMIT].decode("utf-8", errors="replace").split()
        )
        looks_like_html = response.content.lstrip().lower().startswith((b"<!doctype", b"<html"))
        if looks_like_html or "json" not in content_type.lower():
            instructions = (
                "The service answered with a non-JSON page, likely an "
                "interactive authentication or error page. Sign in with "
                "cern_auth_sign_in and retry."
            )
        else:
            instructions = (
                "The service declared JSON but sent an unparseable body; "
                "retry. If it persists, report it with the body snippet."
            )
        raise UpstreamError(
            f"{source} returned a body that could not be parsed as JSON "
            f"(content-type: {content_type or 'unknown'}): {snippet!r}",
            instructions=instructions,
        ) from exc


class _TextExtractor(HTMLParser):
    """Strip small HTML fragments (posts, abstracts) into plain text."""

    _BLOCK_TAGS: ClassVar[frozenset[str]] = frozenset(
        {"p", "br", "div", "li", "pre", "blockquote", "h1", "h2", "h3", "h4", "tr", "td"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script"):
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str, *, limit: int = 20000) -> str:
    """Convert an HTML fragment to whitespace-normalized plain text."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()[:limit]
