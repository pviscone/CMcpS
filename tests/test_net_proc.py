"""Unit tests for the bounded subprocess runner."""

from __future__ import annotations

import sys

import pytest

from cmcps.contracts.errors import UpstreamError
from cmcps.net.proc import run_command

PY = sys.executable


async def test_captures_output_and_exit_code() -> None:
    result = await run_command([PY, "-c", "print('hello')"])
    assert result.exit_code == 0
    assert result.stdout.strip() == b"hello"
    assert result.stderr == b""


async def test_nonzero_exit_and_stderr_passthrough() -> None:
    result = await run_command(
        [PY, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"]
    )
    assert result.exit_code == 3
    assert result.stderr == b"bad"


async def test_missing_executable_maps_upstream() -> None:
    with pytest.raises(UpstreamError, match="executable not found"):
        await run_command(["/nonexistent/cmcps-missing-binary-xyz"])


async def test_timeout_kills_process() -> None:
    with pytest.raises(UpstreamError, match="timed out"):
        await run_command(
            [PY, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.3,
        )


async def test_output_cap_kills_process() -> None:
    with pytest.raises(UpstreamError, match="exceeded"):
        await run_command(
            [PY, "-c", "import sys; sys.stdout.write('x' * (1 << 20))"],
            max_output_bytes=64 * 1024,
        )


async def test_empty_argv_rejected() -> None:
    with pytest.raises(ValueError):
        await run_command([])
