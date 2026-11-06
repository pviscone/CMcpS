"""``cmsmcp login``: sign in to CERN SSO from the terminal with Kerberos + 2FA.

Uses the ``kinit`` ticket instead of the username/password screen and asks for
the 2FA one-time code in the terminal (or takes it from ``--otp``). The session
lands in the same store the MCP server reads, so a running server picks it up
on its next tool call.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections.abc import Callable, Sequence
from datetime import datetime

from cmsmcp.auth.kerberos import KerberosLoginError, OtpProvider, make_kerberos_collector
from cmsmcp.auth.service import AuthService, StorageStateCollector
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import CMSMCPError, InvalidSessionError

_CODE_PATTERN = re.compile(r"\d{6,8}")


def normalise_code(raw: str) -> str:
    """Strip spaces from a one-time code and check it looks like one."""
    code = re.sub(r"\s+", "", raw)
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError("a one-time code is 6-8 digits")
    return code


def make_otp_provider(
    otp: str | None,
    *,
    interactive: bool,
    prompt: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> OtpProvider:
    """Return the code from ``--otp`` once, or prompt in the terminal."""
    used = False

    async def provide(previous_error: str | None) -> str:
        nonlocal used
        if otp is not None:
            if used:
                raise InvalidSessionError(
                    "CERN did not accept the one-time code passed with --otp",
                    instructions="Run `cmsmcp login` again with a fresh code.",
                )
            used = True
            return normalise_code(otp)
        if not interactive:
            raise KerberosLoginError(
                "CERN is asking for a 2FA code but there is no terminal to type it into",
                instructions="Pass the code with `cmsmcp login --otp <code>`.",
            )
        if previous_error:
            say(f"{previous_error}. Try again.")
        while True:
            raw = await asyncio.to_thread(prompt, "CERN 2FA code: ")
            try:
                return normalise_code(raw)
            except ValueError:
                say("Enter the 6-digit code from your authenticator app.")

    return provide


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmsmcp login",
        description=(
            "Sign in to CERN SSO without a browser window: your Kerberos ticket "
            "(from kinit) replaces the password, and the 2FA code is entered here."
        ),
    )
    parser.add_argument(
        "--otp",
        metavar="CODE",
        help="2FA one-time code; use when there is no interactive terminal",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="discard the current session and sign in again even if it is still valid",
    )
    return parser


def run(
    argv: Sequence[str],
    *,
    settings: Settings | None = None,
    interactive: bool | None = None,
    collector_factory: Callable[[OtpProvider], StorageStateCollector] = make_kerberos_collector,
) -> int:
    """Run the login command; returns a process exit code."""
    args = _parser().parse_args(list(argv))
    if args.otp is not None:
        try:
            normalise_code(args.otp)
        except ValueError:
            print("--otp must be the 6-8 digit code from your authenticator app.", file=sys.stderr)
            return 2
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = settings or Settings()
    provider = make_otp_provider(
        args.otp,
        interactive=sys.stdin.isatty() if interactive is None else interactive,
    )
    auth = AuthService(settings, collector=collector_factory(provider))
    try:
        status = auth.status()
        if status.authenticated and not args.force:
            print(
                f"Already signed in to CERN SSO (valid until {_local(status.expires_at)}). "
                "Use --force to sign in again."
            )
            return 0
        result = asyncio.run(auth.reauthenticate() if args.force else auth.sign_in())
    except CMSMCPError as exc:
        print(f"Login failed: {exc.message}", file=sys.stderr)
        if exc.instructions:
            print(exc.instructions, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nLogin cancelled.", file=sys.stderr)
        return 130
    print(f"Signed in to CERN SSO. Session valid until {_local(result.expires_at)}.")
    return 0


def _local(moment: datetime | None) -> str:
    if moment is None:
        return "an unknown time"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")
