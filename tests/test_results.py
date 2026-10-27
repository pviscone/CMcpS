"""ToolEnvelope transport-budget guard: oversize results must fail cleanly."""

from __future__ import annotations

from cmcps.contracts.results import (
    MAX_ENVELOPE_JSON_BYTES,
    ErrorCode,
    ToolEnvelope,
)


def test_envelope_rejects_oversize_result() -> None:
    env = ToolEnvelope.success({"big": "x" * (MAX_ENVELOPE_JSON_BYTES + 1024)})
    assert env.status == "error"
    assert env.error is not None
    assert env.error.code == ErrorCode.REQUEST_TOO_LARGE
    assert env.data is None


def test_envelope_accepts_small_result() -> None:
    env = ToolEnvelope.success({"ok": True})
    assert env.status == "ok"
    assert env.data == {"ok": True}
