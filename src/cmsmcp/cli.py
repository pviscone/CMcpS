"""``cmsmcp`` command line: serve, login, setup, status, doctor, logout, audit, approve.

``cmsmcp`` with no arguments runs the MCP server over stdio (what MCP clients
launch). Everything else is for the human at the terminal:

- ``setup [step...]``: guided pass over every credential (SSO, Kerberos,
  lxplus, grid proxy, personal API tokens, personal-data and action
  settings). Each step says what it unlocks, opens the page where the token is
  created, reads secrets without echo, verifies, and stores them in private
  files under ``~/.config/cmsmcp``. Every step can be skipped.
- ``status`` / ``doctor``: what works right now (``doctor`` also contacts the
  services and checks token scopes).
- ``logout``: remove stored credentials (``--purge`` also clears logs/caches).
- ``audit``: show the local log of tool calls.
- ``approve``: review and approve actions an agent parked for confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings, config_file, secrets_dir
from cmsmcp.credentials import (
    FAIL,
    OK,
    TOKENS,
    TOKENS_BY_NAME,
    Check,
    TokenSpec,
    check_grid,
    check_kerberos,
    check_lxplus,
    check_sso,
    check_token,
    format_status,
    gather_status,
)
from cmsmcp.policy import PendingApprovals

COMMANDS = ("login", "setup", "status", "doctor", "logout", "audit", "approve", "serve")
# Core: what nearly every tool needs. Everything else is optional and is set up
# on demand: a tool that needs a missing credential answers with the exact
# `cmsmcp setup <step>` to run, so nothing is asked up front.
# Three tiers: `cmsmcp setup` = CERN SSO only; `--tokens` adds the long-lived
# personal API tokens; `--all` adds Kerberos, lxplus, grid, Rucio and the
# personal-data/actions policy.
CORE_STEPS = ("sso",)
TOKEN_STEPS = tuple(t.name for t in TOKENS)
FULL_ONLY_STEPS = ("kerberos", "lxplus", "grid", "rucio", "policy")
OPTIONAL_STEPS = TOKEN_STEPS + FULL_ONLY_STEPS
SETUP_STEPS = CORE_STEPS + OPTIONAL_STEPS
TIERS = {"sso": CORE_STEPS, "tokens": CORE_STEPS + TOKEN_STEPS, "all": SETUP_STEPS}
OPTIONAL_GUIDE = {
    "kerberos": "kinit ticket (lxplus, EOS, browser-free SSO login)",
    "lxplus": "run read-only queries on lxplus over Kerberos",
    "grid": "grid certificate / VOMS proxy (DAS, DQM GUI, Rucio)",
    **{t.name: t.unlocks for t in TOKENS},
    "rucio": "your Rucio account name",
    "policy": "opt in to personal data (Mattermost DMs) and enable confirmed actions",
}
_WRITE_SCOPES = frozenset({"api", "write_repository", "sudo", "admin_mode"})


@dataclass
class IO:
    """Terminal I/O, swappable in tests."""

    ask: Callable[[str], str] = input
    secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print
    open_url: Callable[[str], object] = webbrowser.open
    run: Callable[[list[str]], int] = field(
        default=lambda argv: subprocess.run(argv, check=False).returncode  # noqa: S603
    )


# ------------------------------------------------------------------ storage
def write_secret(name: str, value: str) -> Path:
    directory = secrets_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value.strip() + "\n")
    path.chmod(0o600)
    return path


def set_option(key: str, value: str | None) -> None:
    """Set (or with ``None`` remove) one ``CMSMCP_*`` line in the config file."""
    path = config_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    env_key = f"CMSMCP_{key.upper()}"
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(f"{env_key}=")]
    if value is not None:
        lines.append(f"{env_key}={value}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(lines) + ("\n" if lines else ""))


def _yes(io: IO, prompt: str, *, default: bool) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = io.ask(prompt + suffix).strip().lower()
    return default if not answer else answer.startswith("y")


# ------------------------------------------------------------- live checks
async def live_token_check(settings: Settings, spec: TokenSpec) -> Check:  # noqa: PLR0911
    """Contact the service with the token; never prints it."""
    from cmsmcp.net.client import SafeHttpClient  # noqa: PLC0415 - keep CLI startup light

    token = getattr(settings, spec.field)
    if not token:
        return check_token(settings, spec)
    probes: dict[str, tuple[str, dict[str, str]]] = {
        "gitlab": (
            f"{settings.gitlab_base_url}/api/v4/personal_access_tokens/self",
            {"PRIVATE-TOKEN": token},
        ),
        "indico": (f"{settings.indico_base_url}/api/user/", {"Authorization": f"Bearer {token}"}),
        "jira": (
            "https://its.cern.ch/jira/rest/api/2/myself",
            {"Authorization": f"Bearer {token}"},
        ),
        "mattermost": (
            f"{settings.mattermost_base_url}/api/v4/users/me",
            {"Authorization": f"Bearer {token}"},
        ),
        "github": ("https://api.github.com/user", {"Authorization": f"Bearer {token}"}),
    }
    url, headers = probes[spec.name]
    async with SafeHttpClient(settings) as client:
        try:
            response = await client.request(
                "GET", url, authenticated=False, service_headers=headers
            )
        except Exception as exc:
            return Check(FAIL, f"could not reach {spec.title}: {exc}")
    if response.status_code != 200:  # noqa: PLR2004
        return Check(FAIL, f"{spec.title} rejected the token (HTTP {response.status_code})")
    if spec.name != "gitlab":
        return Check(OK, "token accepted")
    try:
        info = json.loads(response.content)
    except ValueError:
        return Check(OK, "token accepted")
    scopes = set(info.get("scopes") or [])
    expires = info.get("expires_at") or "never"
    writes = scopes & _WRITE_SCOPES
    if writes and not settings.actions_enabled:
        return Check(
            FAIL,
            f"token has write scopes {sorted(writes)} but actions are disabled: "
            "create a read_api token instead",
        )
    return Check(OK, f"scopes {', '.join(sorted(scopes))}; expires {expires}")


# ------------------------------------------------------------------- setup
class Setup:
    """The guided setup steps."""

    def __init__(self, io: IO, settings_factory: Callable[[], Settings] = Settings) -> None:
        self.io = io
        self.settings_factory = settings_factory

    @property
    def settings(self) -> Settings:
        return self.settings_factory()

    def run(self, steps: Sequence[str], *, tier: str = "sso") -> int:
        chosen = list(steps) or list(TIERS[tier])
        unknown = [s for s in chosen if s not in SETUP_STEPS]
        if unknown:
            self.io.say(
                f"Unknown setup step(s): {', '.join(unknown)}. Steps: {', '.join(SETUP_STEPS)}"
            )
            return 2
        self.io.say(
            "cmsmcp setup: connect the CERN services you use. Every step can be skipped;\n"
            f"secrets go to {secrets_dir()} (owner-only), options to {config_file()}.\n"
        )
        for step in chosen:
            self.io.say(f"── {step} " + "─" * max(0, 60 - len(step)))
            if step in TOKENS_BY_NAME:
                self.step_token(TOKENS_BY_NAME[step])
            else:
                getattr(self, f"step_{step}")()
        self.io.say(
            "\nSummary:\n" + format_status(asyncio.run(gather_status(self.settings, live=False)))
        )
        skipped = [step for step in OPTIONAL_STEPS if step not in chosen]
        if skipped:
            self.io.say(
                "\nOptional extras (set up any time; a tool that needs one will tell you):\n"
                + "\n".join(f"  cmsmcp setup {s:<11} {OPTIONAL_GUIDE[s]}" for s in skipped)
                + "\n  cmsmcp setup --tokens    all personal API tokens"
                + "\n  cmsmcp setup --all       everything (tokens, Kerberos, lxplus, grid, ...)"
            )
        self.io.say(
            "\nAdd cmsmcp to your MCP client, e.g. Claude Code:\n"
            "  claude mcp add cmsmcp -- uvx --from <path-or-git-url> cmsmcp\n"
            "Run `cmsmcp doctor` any time to test every connection."
        )
        return 0

    def _keep(self, check: Check) -> bool:
        self.io.say(f"  status: {check.state} - {check.detail}")
        return check.state == OK and _yes(self.io, "  Keep as is?", default=True)

    def step_sso(self) -> None:
        self.io.say("CERN SSO: needed by almost every tool (Indico, TWiki, GLANCE, CMS Talk, ...).")
        for problem in dependency_problems(self.settings):
            self.io.say(f"  note: {problem}")
        if self._keep(check_sso(self.settings)):
            return
        choice = (
            self.io.ask(
                "  Sign in with [k] Kerberos ticket + 2FA code in this terminal, "
                "[b] a browser window, or [s] skip? "
            )
            .strip()
            .lower()
        )
        if choice.startswith("k"):
            from cmsmcp.login import run as login_run  # noqa: PLC0415

            if asyncio.run(check_kerberos(self.settings)).state != OK:
                self.step_kerberos()
            login_run([], settings=self.settings)
        elif choice.startswith("b"):
            try:
                asyncio.run(AuthService(self.settings).sign_in())
                self.io.say("  Signed in.")
            except Exception as exc:
                self.io.say(f"  Browser sign-in failed: {exc}")

    def step_kerberos(self) -> None:
        self.io.say(
            "Kerberos: a `kinit` ticket gives password-less lxplus/EOS access and lets "
            "`cmsmcp login` replace the SSO password screen."
        )
        if self._keep(asyncio.run(check_kerberos(self.settings))):
            return
        if shutil.which("kinit") is None:
            self.io.say(
                "  Install the Kerberos client (krb5-workstation or krb5-user), then rerun."
            )
            return
        user = self.settings.lxplus_user or os.environ.get("USER", "")
        principal = self.io.ask(f"  CERN principal [{user}@CERN.CH]: ").strip() or f"{user}@CERN.CH"
        if "@" not in principal:
            principal = f"{principal}@CERN.CH"
        self.io.run(["kinit", principal])

    def step_lxplus(self) -> None:
        self.io.say(
            "lxplus: run read-only queries (EOS, HTCondor, quotas) over your Kerberos ticket."
        )
        settings = self.settings
        if settings.lxplus_user and self._keep(asyncio.run(check_lxplus(settings))):
            return
        default = settings.lxplus_user or os.environ.get("USER", "")
        user = self.io.ask(f"  CERN username [{default}] (- to skip): ").strip() or default
        if user == "-":
            return
        set_option("lxplus_user", user)
        result = asyncio.run(check_lxplus(self.settings))
        self.io.say(f"  {result.state}: {result.detail}")

    def step_grid(self) -> None:
        self.io.say("Grid: an X.509 certificate/VOMS proxy for DAS, the DQM GUI and Rucio.")
        settings = self.settings
        if self._keep(asyncio.run(check_grid(settings))):
            return
        if not settings.grid_cert_file.is_file():
            self.io.say(
                "  Get a CERN grid certificate at https://ca.cern.ch, convert it to "
                "~/.globus/usercert.pem + userkey.pem (chmod 400 the key), then rerun."
            )
            return
        if shutil.which("voms-proxy-init") is None:
            self.io.say("  voms-proxy-init is not installed here (it is on lxplus).")
            return
        if _yes(
            self.io,
            "  Create a CMS VOMS proxy now (asks your certificate passphrase)?",
            default=True,
        ):
            self.io.run(["voms-proxy-init", "-voms", "cms", "-rfc", "-valid", "192:00"])
            proxy = os.environ.get("X509_USER_PROXY") or f"/tmp/x509up_u{os.getuid()}"  # noqa: S108
            if Path(proxy).is_file():
                set_option("grid_cert_file", proxy)
                set_option("grid_key_file", proxy)
                self.io.say(f"  Using the proxy {proxy} for grid services.")

    def step_token(self, spec: TokenSpec) -> None:
        self.io.say(f"{spec.title}: {spec.unlocks}.")
        current = check_token(self.settings, spec)
        if self._keep(current):
            return
        if not _yes(self.io, f"  Set up a {spec.title} token now?", default=not spec.optional):
            return
        self.io.say(f"  {spec.instructions}\n  Page: {spec.url}")
        if _yes(self.io, "  Open it in your browser?", default=True):
            self.io.open_url(spec.url)
        value = self.io.secret("  Paste the token (input hidden, empty to skip): ").strip()
        if not value:
            return
        if spec.prefix and not value.startswith(spec.prefix):
            self.io.say(f"  Warning: {spec.title} tokens normally start with {spec.prefix}.")
        write_secret(spec.field, value)
        verdict = asyncio.run(live_token_check(self.settings, spec))
        self.io.say(f"  {verdict.state}: {verdict.detail}")
        if verdict.state == FAIL and _yes(self.io, "  Remove the stored token?", default=True):
            (secrets_dir() / spec.field).unlink(missing_ok=True)

    def step_rucio(self) -> None:
        self.io.say("Rucio: your CMS data rules and datasets (uses the grid proxy).")
        default = (
            self.settings.rucio_account or self.settings.lxplus_user or os.environ.get("USER", "")
        )
        account = self.io.ask(f"  Rucio account [{default}] (- to skip): ").strip() or default
        if account and account != "-":
            set_option("rucio_account", account)

    def step_policy(self) -> None:
        settings = self.settings
        self.io.say(
            "Personal data and actions.\n"
            "  Personal sources (e.g. your Mattermost direct messages) stay hidden from the agent\n"
            "  unless you opt in. Actions (posting, commenting, submitting jobs) are off by\n"
            "  default and, when on, each one asks for your confirmation."
        )
        dms = "mattermost_dm" in settings.personal_sources
        if _yes(
            self.io, "  Let the agent read your Mattermost direct/group messages?", default=dms
        ):
            set_option("personal_sources", "mattermost_dm")
        else:
            set_option("personal_sources", None)
        if _yes(
            self.io, "  Enable actions (each confirmed by you)?", default=settings.actions_enabled
        ):
            set_option("actions_enabled", "true")
            mode = (
                self.io.ask(
                    "  Confirm actions [e] in the MCP client (falls back to `cmsmcp approve`) or "
                    "[a] always in the terminal with `cmsmcp approve`? [e] "
                )
                .strip()
                .lower()
            )
            set_option("confirm_mode", "approve" if mode.startswith("a") else "elicit")
        else:
            set_option("actions_enabled", None)
            set_option("confirm_mode", None)


def dependency_problems(settings: Settings) -> list[str]:
    """Local prerequisites of the core sign-in paths that are missing."""
    problems = []
    if settings.browser_executable is None:
        problems.append(
            "no Chromium/Chrome found for browser sign-in and SSO-rendered pages: install "
            "one, or run `uvx playwright install chromium` and set CMSMCP_BROWSER_EXECUTABLE"
        )
    try:
        import gssapi  # noqa: F401, PLC0415
    except ImportError:
        problems.append(
            "Kerberos login needs the optional extra: install with `cmsmcp[kerberos]` "
            "(needs libkrb5-dev); browser sign-in works without it"
        )
    return problems


# --------------------------------------------------------------- commands
def cmd_status(settings: Settings, *, live: bool) -> int:
    rows = asyncio.run(gather_status(settings, live=live))
    if live:
        tokens = [
            (spec.name, asyncio.run(live_token_check(settings, spec)))
            for spec in TOKENS
            if getattr(settings, spec.field)
        ]
        rows = [r for r in rows if r[0] not in {n for n, _ in tokens}] + tokens
    print(format_status(rows))
    return 1 if any(c.state == FAIL for _, c in rows) else 0


def cmd_logout(settings: Settings, names: Sequence[str], *, everything: bool, purge: bool) -> int:
    targets = set(names) or (set(SETUP_STEPS) if everything else set())
    if not targets and not purge:
        print("Name what to remove (e.g. `cmsmcp logout gitlab`), or use --all.")
        return 2
    if "sso" in targets:
        auth = AuthService(settings)
        auth.store.clear()
        auth.service_token_store().clear_all()
        print("Removed the CERN SSO session and service tokens.")
    for spec in TOKENS:
        if spec.name in targets and (secrets_dir() / spec.field).exists():
            (secrets_dir() / spec.field).unlink()
            print(f"Removed the {spec.title} token.")
    if purge:
        for leftover in ("audit.jsonl", "pending-actions.json", "websites-inventory.db"):
            (settings.state_dir / leftover).unlink(missing_ok=True)
        if settings.download_dir.exists():
            shutil.rmtree(settings.download_dir)
        print("Purged the audit log, pending approvals, caches and downloads.")
    return 0


def cmd_audit(settings: Settings, count: int) -> int:
    path = settings.state_dir / "audit.jsonl"
    if not path.exists():
        print("No tool calls recorded yet.")
        return 0
    for line in path.read_text().splitlines()[-count:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        args = ", ".join(f"{k}={v}" for k, v in rec.get("args", {}).items())
        print(f"{rec.get('ts')} {rec.get('status'):<16} {rec.get('tool')}({args})")
    return 0


def cmd_approve(settings: Settings, key: str | None, *, deny: bool, io: IO | None = None) -> int:
    io = io or IO()
    queue = PendingApprovals(settings.state_dir / "pending-actions.json")
    entries = {k: v for k, v in queue.load().items() if not v.get("used")}
    if key:
        entries = {k: v for k, v in entries.items() if k == key}
        if not entries:
            io.say(f"No pending action {key!r} (expired after 15 minutes, or already used).")
            return 1
    if not entries:
        io.say("No actions are waiting for approval.")
        return 0
    for name, entry in entries.items():
        age = int(time.time() - float(entry.get("created", 0)))
        io.say(
            f"\n[{name}] {entry.get('tool')}: {entry.get('summary')}  ({age}s ago)\n"
            + json.dumps(entry.get("details"), indent=2, ensure_ascii=False)
        )
        if entry.get("approved"):
            io.say("  already approved; the agent can run it.")
            continue
        approve = False if deny else _yes(io, "  Approve this exact action?", default=False)
        queue.set_approved(name, approve)
        io.say(
            "  approved: tell the agent to retry with approval_id=" + name
            if approve
            else "  declined."
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cmsmcp", description="CERN/CMS MCP server and tools.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")
    sub.add_parser("login", help="CERN SSO via Kerberos + 2FA in the terminal", add_help=False)
    setup = sub.add_parser(
        "setup", help="guided setup: CERN SSO by default; --tokens or --all for more"
    )
    setup.add_argument("steps", nargs="*", help=f"optional steps: {', '.join(OPTIONAL_STEPS)}")
    tier = setup.add_mutually_exclusive_group()
    tier.add_argument(
        "--tokens",
        action="store_const",
        const="tokens",
        dest="tier",
        help="SSO + long-lived API tokens (GitLab, Indico, Jira, Mattermost, GitHub)",
    )
    tier.add_argument(
        "--all",
        action="store_const",
        const="all",
        dest="tier",
        help="everything: tokens, Kerberos, lxplus, grid, Rucio, personal data/actions",
    )
    sub.add_parser("status", help="what is configured (local checks)")
    sub.add_parser("doctor", help="test every connection and token")
    logout = sub.add_parser("logout", help="remove stored credentials")
    logout.add_argument("names", nargs="*", help="e.g. sso gitlab")
    logout.add_argument("--all", action="store_true", dest="everything")
    logout.add_argument("--purge", action="store_true", help="also clear logs, caches, downloads")
    audit = sub.add_parser("audit", help="recent tool calls")
    audit.add_argument("-n", type=int, default=40)
    approve = sub.add_parser("approve", help="review actions waiting for your approval")
    approve.add_argument("id", nargs="?")
    approve.add_argument("--deny", action="store_true")
    return parser


def main(argv: Sequence[str]) -> int:  # noqa: PLR0911
    if argv[:1] == ["login"]:
        from cmsmcp.login import run as login_run  # noqa: PLC0415

        return login_run(argv[1:])
    args = _parser().parse_args(list(argv))
    settings = Settings()
    if args.command == "setup":
        return Setup(IO()).run(args.steps, tier=args.tier or "sso")
    if args.command in {"status", "doctor"}:
        return cmd_status(settings, live=args.command == "doctor")
    if args.command == "logout":
        return cmd_logout(settings, args.names, everything=args.everything, purge=args.purge)
    if args.command == "audit":
        return cmd_audit(settings, args.n)
    if args.command == "approve":
        return cmd_approve(settings, args.id, deny=args.deny)
    print("Run `cmsmcp` without arguments to start the MCP server.", file=sys.stderr)
    return 2
