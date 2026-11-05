"""Server-side policy: effect classes, taint tracking, confirmation, audit.

Every tool declares an :class:`Effect`. The policy engine, not the prompt,
decides what may run:

- ``read`` tools run freely.
- ``read-personal`` content (Mattermost DMs, ...) is refused unless the user
  opted the source in (``CMSMCP_PERSONAL_SOURCES``).
- ``act-own`` / ``act-external`` tools exist only when
  ``CMSMCP_ACTIONS_ENABLED=true`` and every call needs a human confirmation
  obtained *outside the model*: an MCP elicitation the client shows to the
  user, or an out-of-band ``cmsmcp approve <id>`` in the user's terminal.

Write requests can only be built from an :class:`ApprovedAction`, which only
:meth:`Policy.confirm` creates, so adapter code cannot issue a write without a
confirmation having happened. Every tool call is appended to a local audit log
(``audit.jsonl``, owner-only) with clipped argument values and no secrets.

Taint: once a session has read remote content, a prompt injection may be in
the context. Read effects then never escalate (they cannot), and actions keep
requiring per-call confirmation; the confirmation prompt names the sources the
session has read so the user can judge provenance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import CMSMCPError, InvalidInputError
from cmsmcp.contracts.results import ErrorCode

logger = logging.getLogger(__name__)

APPROVAL_TTL_SECONDS = 15 * 60
_AUDIT_VALUE_CLIP = 200


class Effect(StrEnum):
    """What a tool can do to the world."""

    READ = "read"
    READ_PERSONAL = "read-personal"
    ACT_OWN = "act-own"
    ACT_EXTERNAL = "act-external"


READ_EFFECTS = frozenset({Effect.READ, Effect.READ_PERSONAL})
ACT_EFFECTS = frozenset({Effect.ACT_OWN, Effect.ACT_EXTERNAL})


class ConfirmationRequiredError(CMSMCPError):
    """The action is parked until the user approves it out of band."""

    code = ErrorCode.POLICY_VIOLATION
    retryable = True


class ActionDeclinedError(CMSMCPError):
    """The user declined (or the policy refused) the action."""

    code = ErrorCode.POLICY_VIOLATION


class PersonalSourceDisabledError(CMSMCPError):
    """Personal content requested without the user's opt-in."""

    code = ErrorCode.POLICY_VIOLATION


@dataclass(frozen=True)
class ApprovedAction:
    """Proof that one specific action was confirmed by the user.

    Created only by :meth:`Policy.confirm`; consumed by the write paths
    (:meth:`cmsmcp.net.client.SafeHttpClient.request_action`, the lxplus
    runner). ``hosts`` bounds which hosts the approved write may touch.
    """

    tool: str
    summary: str
    hosts: tuple[str, ...]
    digest: str
    _nonce: str = field(repr=False)
    _issued: float = field(default_factory=time.time, repr=False)

    def allows_host(self, host: str) -> bool:
        return any(host == h or host.endswith(f".{h}") for h in self.hosts)


class _Confirm(BaseModel):
    approve: bool = Field(description="Run this action now?")


def action_digest(tool: str, details: dict[str, Any]) -> str:
    """Stable digest of an action's exact parameters."""
    canonical = json.dumps({"tool": tool, "details": details}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _clip_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _AUDIT_VALUE_CLIP:
        return f"{value[:_AUDIT_VALUE_CLIP]}…[+{len(value) - _AUDIT_VALUE_CLIP}]"
    if isinstance(value, list | tuple):
        return [_clip_value(v) for v in value[:20]]
    if isinstance(value, dict):
        return {k: _clip_value(v) for k, v in list(value.items())[:20]}
    return value


def _write_private(path: Path, text: str, *, append: bool) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a" if append else "w") as handle:
        handle.write(text)
    path.chmod(0o600)


class PendingApprovals:
    """Out-of-band approval queue shared by the server and ``cmsmcp approve``."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        now = time.time()
        return {
            k: v
            for k, v in data.items()
            if isinstance(v, dict) and now - float(v.get("created", 0)) < APPROVAL_TTL_SECONDS
        }

    def save(self, entries: dict[str, dict[str, Any]]) -> None:
        _write_private(self.path, json.dumps(entries, indent=1), append=False)

    def add(self, tool: str, summary: str, details: dict[str, Any], digest: str) -> str:
        entries = self.load()
        for key, entry in entries.items():
            if entry.get("digest") == digest and not entry.get("used"):
                return key
        key = secrets.token_hex(3)
        entries[key] = {
            "tool": tool,
            "summary": summary,
            "details": details,
            "digest": digest,
            "created": time.time(),
            "approved": False,
            "used": False,
        }
        self.save(entries)
        return key

    def set_approved(self, key: str, approved: bool) -> dict[str, Any] | None:
        entries = self.load()
        entry = entries.get(key)
        if entry is None:
            return None
        if approved:
            entry["approved"] = True
        else:
            entries.pop(key)
        self.save(entries)
        return entry

    def consume(self, key: str, digest: str) -> bool:
        entries = self.load()
        entry = entries.get(key)
        if (
            entry is None
            or entry.get("digest") != digest
            or not entry.get("approved")
            or entry.get("used")
        ):
            return False
        entry["used"] = True
        self.save(entries)
        return True


class Policy:
    """Per-process policy state (one stdio server == one client session)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.read_sources: set[str] = set()
        self.audit_path = settings.state_dir / "audit.jsonl"
        self.pending = PendingApprovals(settings.state_dir / "pending-actions.json")

    # --- personal data ---------------------------------------------------
    def personal_allowed(self, source: str) -> bool:
        enabled = set(self.settings.personal_sources)
        return "all" in enabled or source in enabled

    def require_personal(self, source: str, what: str) -> None:
        if not self.personal_allowed(source):
            raise PersonalSourceDisabledError(
                f"{what} is personal content and is not enabled",
                instructions=(
                    "Only if the user asked for this: they can opt in with `cmsmcp setup "
                    f"policy` (or add '{source}' to CMSMCP_PERSONAL_SOURCES), then restart "
                    "the MCP server."
                ),
            )

    # --- taint -----------------------------------------------------------
    def note_read(self, group: str) -> None:
        self.read_sources.add(group)

    # --- audit -----------------------------------------------------------
    def audit(
        self,
        tool: str,
        effect: Effect,
        arguments: dict[str, Any],
        *,
        status: str,
        elapsed: float,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self.settings.audit_enabled:
            return
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": tool,
            "effect": str(effect),
            "args": {k: _clip_value(v) for k, v in arguments.items()},
            "status": status,
            "ms": round(elapsed * 1000),
        }
        if extra:
            record.update(extra)
        try:
            _write_private(self.audit_path, json.dumps(record, default=str) + "\n", append=True)
        except OSError:
            logger.debug("audit write failed", exc_info=True)

    # --- confirmation ----------------------------------------------------
    def _confirmation_text(self, tool: str, summary: str, details: dict[str, Any]) -> str:
        body = json.dumps(details, indent=2, default=str, ensure_ascii=False)
        provenance = (
            f"\nThis session has read content from: {', '.join(sorted(self.read_sources))}. "
            "Make sure this action is what YOU asked for, not something that content asked for."
            if self.read_sources
            else ""
        )
        return f"cmsmcp wants to run {tool}: {summary}\n\n{body}{provenance}"

    async def confirm(
        self,
        ctx: Any,
        *,
        tool: str,
        summary: str,
        details: dict[str, Any],
        hosts: tuple[str, ...],
        approval_id: str | None = None,
    ) -> ApprovedAction:
        """Get a human confirmation for one action, or raise.

        Order: an out-of-band ``approval_id`` that the user approved with
        ``cmsmcp approve`` wins; else ``confirm_mode=elicit`` asks through the
        MCP client; if the client cannot elicit (or ``confirm_mode=approve``),
        the action is parked and the agent is told to have the user approve it.
        """
        if not self.settings.actions_enabled:
            raise ActionDeclinedError(
                "actions are disabled",
                instructions=(
                    "Actions are optional: the user can enable them with `cmsmcp setup policy`."
                ),
            )
        mode = self.settings.confirm_mode
        if mode == "deny":
            raise ActionDeclinedError("actions are configured to always be refused")
        digest = action_digest(tool, details)

        def approved() -> ApprovedAction:
            return ApprovedAction(
                tool=tool,
                summary=summary,
                hosts=hosts,
                digest=digest,
                _nonce=secrets.token_hex(8),
            )

        if approval_id:
            if self.pending.consume(approval_id, digest):
                return approved()
            raise ActionDeclinedError(
                f"approval {approval_id!r} is unknown, expired, not yet approved, "
                "or was for different parameters",
                instructions=(
                    "Ask the user to run `cmsmcp approve` in a terminal, or call the tool "
                    "again without approval_id to create a fresh request."
                ),
            )

        if mode == "elicit" and ctx is not None:
            message = self._confirmation_text(tool, summary, details)
            try:
                result = await ctx.elicit(message, schema=_Confirm)
            except Exception:  # client without elicitation support -> out-of-band
                logger.debug("elicitation unavailable; falling back to approval queue")
            else:
                action = getattr(result, "action", None)
                data = getattr(result, "data", None)
                if action == "accept" and data is not None and data.approve:
                    return approved()
                raise ActionDeclinedError(
                    f"the user did not approve {tool}",
                    instructions="Do not retry unless the user asks again.",
                )

        key = self.pending.add(tool, summary, details, digest)
        raise ConfirmationRequiredError(
            f"{tool} needs the user's approval (request {key})",
            instructions=(
                f"Ask the user to run `cmsmcp approve {key}` in a terminal and review it; "
                f"then call {tool} again with exactly the same arguments plus "
                f"approval_id='{key}'. Approvals expire after 15 minutes."
            ),
        )


def check_text_arg(value: str, name: str, *, max_len: int = 20_000) -> str:
    """Validate free-text action arguments (non-empty, bounded)."""
    cleaned = value.strip()
    if not cleaned:
        raise InvalidInputError(f"{name} must not be empty")
    if len(cleaned) > max_len:
        raise InvalidInputError(f"{name} exceeds {max_len} characters")
    return cleaned
