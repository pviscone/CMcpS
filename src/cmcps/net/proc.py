"""Bounded asynchronous subprocess execution for local data tools.

Used by adapters that delegate to local CLIs (currently ``dasgoclient`` for
CMS DAS). Mirrors the HTTP client's policy posture in a subprocess world:
no shell (list argv only), explicit environment, wall-clock timeout, and
capped stdout/stderr capture.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from cmcps.contracts.errors import UpstreamError

_CHUNK = 64 * 1024

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_OUTPUT_BYTES = 10 * 1024 * 1024


class _OutputTooLargeError(Exception):
    """Internal: a captured stream exceeded the configured byte cap."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Completed subprocess result."""

    exit_code: int
    stdout: bytes
    stderr: bytes


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    buffer = bytearray()
    while chunk := await stream.read(_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > cap:
            raise _OutputTooLargeError
    return bytes(buffer)


async def _drain(stream: asyncio.StreamReader) -> None:
    """Discard a stream until EOF, tolerating transport races."""
    try:
        while await stream.read(_CHUNK):
            pass
    except Exception:  # transport torn down mid-drain; nothing left to read
        return


async def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> CommandResult:
    """Run ``argv`` without a shell, killing on timeout or output overflow.

    Raises:
        UpstreamError: executable missing, timeout, or output over the cap.
    """
    if not argv:
        raise ValueError("run_command requires a non-empty argv")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=dict(env) if env is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise UpstreamError(
            f"executable not found: {argv[0]}",
            instructions=(
                "Install the tool or configure its absolute path via the "
                "matching CMCPS_ environment setting."
            ),
        ) from exc
    assert proc.stdout is not None and proc.stderr is not None

    out_task = asyncio.create_task(_read_capped(proc.stdout, max_output_bytes))
    err_task = asyncio.create_task(_read_capped(proc.stderr, max_output_bytes))
    capture = asyncio.gather(out_task, err_task)
    try:
        stdout, stderr = await asyncio.wait_for(capture, timeout=timeout_seconds)
        exit_code = await proc.wait()
        return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    except (TimeoutError, _OutputTooLargeError) as exc:
        if isinstance(exc, _OutputTooLargeError):
            raise_fail = UpstreamError(
                f"{argv[0]} output exceeded the {max_output_bytes} byte limit",
                instructions="Narrow the query or lower the limit.",
            )
        else:
            raise_fail = UpstreamError(
                f"{argv[0]} timed out after {timeout_seconds:g}s",
                instructions="Retry; if it persists, narrow the query.",
            )
            raise_fail.retryable = True
        raise raise_fail from exc
    finally:
        # Teardown order matters on CPython 3.13 (unix): after kill(),
        # proc.wait() only returns once the pipe transports have been drained
        # to EOF; waiting with paused-but-readers-cancelled pipes hangs
        # indefinitely. So: stop the readers, kill, drain both pipes, then
        # reap.
        out_task.cancel()
        err_task.cancel()
        await asyncio.gather(out_task, err_task, return_exceptions=True)
        if proc.returncode is None:
            proc.kill()
        await asyncio.gather(_drain(proc.stdout), _drain(proc.stderr))
        await proc.wait()
