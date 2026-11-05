"""Inventory and health checks for every credential cmsmcp can use.

One place answers "what can this installation reach right now?":

- ``sso``: the stored CERN SSO session (browser or ``cmsmcp login``).
- ``kerberos``: a ``kinit`` ticket (lxplus, EOS, silent SSO renewal).
- ``lxplus``: password-less SSH to lxplus over the Kerberos ticket.
- ``grid``: X.509 user certificate / VOMS proxy (DAS, DQM, Rucio).
- personal tokens: GitLab, Indico, Jira, Mattermost, GitHub.

Checks never print or return secret values. ``quick`` checks are local (file
presence, ``klist``); ``live`` checks contact the service (``cmsmcp doctor``).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings, secrets_dir
from cmsmcp.net.proc import run_command

OK, WARN, MISSING, FAIL = "ok", "warn", "missing", "fail"


@dataclass(frozen=True)
class Check:
    state: str
    detail: str


@dataclass(frozen=True)
class TokenSpec:
    """A personal API token the user creates themselves."""

    name: str
    field: str
    title: str
    unlocks: str
    url: str
    instructions: str
    prefix: str = ""
    optional: bool = True


TOKENS: tuple[TokenSpec, ...] = (
    TokenSpec(
        "gitlab",
        "gitlab_token",
        "CERN GitLab",
        "private/internal repos, code search, issues, merge requests, pipelines",
        "https://gitlab.cern.ch/-/user_settings/personal_access_tokens",
        "Add new token, name it cmsmcp, set an expiry. Scope read_api for reading; "
        "add api only if you enable actions (comments, issues). It starts with glpat-.",
        prefix="glpat-",
    ),
    TokenSpec(
        "indico",
        "indico_api_token",
        "Indico",
        "protected events, categories and attachments",
        "https://indico.cern.ch/user/tokens/",
        "Create new token, name it cmsmcp, tick read:everything and read:legacy_api only. "
        "It starts with indp_.",
        prefix="indp_",
    ),
    TokenSpec(
        "jira",
        "jira_token",
        "CERN Jira",
        "restricted Jira projects without relying on the browser session",
        "https://its.cern.ch/jira/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens",
        "Profile -> Personal Access Tokens -> Create token, name it cmsmcp, set an expiry.",
    ),
    TokenSpec(
        "mattermost",
        "mattermost_token",
        "Mattermost",
        "a session-independent Mattermost login (the SSO session already works)",
        "https://mattermost.web.cern.ch",
        "Profile -> Security -> Personal Access Tokens (only if your account may create them). "
        "Without one, cmsmcp uses the SSO session.",
    ),
    TokenSpec(
        "github",
        "github_token",
        "GitHub",
        "higher rate limits and code search for cms-sw repositories",
        "https://github.com/settings/personal-access-tokens/new",
        "Fine-grained token, name cmsmcp, Repository access: Public repositories, leave all "
        "permissions at No access. It starts with github_pat_.",
        prefix="github_pat_",
    ),
)
TOKENS_BY_NAME = {spec.name: spec for spec in TOKENS}


# ---------------------------------------------------------------- local checks
def check_sso(settings: Settings) -> Check:
    meta = AuthService(settings).status()
    if meta.authenticated and meta.expires_at:
        return Check(OK, f"signed in, expires {meta.expires_at:%Y-%m-%d %H:%M} UTC")
    if meta.authenticated:
        return Check(OK, "signed in")
    if meta.session_exists:
        return Check(WARN, "session expired: run `cmsmcp login` or `cmsmcp setup sso`")
    return Check(MISSING, "not signed in: run `cmsmcp login` (Kerberos) or `cmsmcp setup sso`")


async def check_kerberos(settings: Settings) -> Check:  # noqa: ARG001 - uniform signature
    klist = shutil.which("klist")
    if klist is None:
        return Check(MISSING, "klist not installed (install krb5-workstation / krb5-user)")
    valid = await run_command([klist, "-s"], timeout_seconds=10)
    listing = await run_command([klist], timeout_seconds=10)
    principal = re.search(rb"[Pp]rincipal:\s*(\S+)", listing.stdout)
    who = principal.group(1).decode() if principal else "unknown principal"
    if valid.exit_code == 0:
        return Check(OK, f"ticket for {who}")
    if principal:
        return Check(WARN, f"ticket for {who} expired: run `kinit {who}`")
    return Check(MISSING, "no ticket: run `kinit <user>@CERN.CH`")


def _proxy_path() -> Path:
    explicit = os.environ.get("X509_USER_PROXY")
    return Path(explicit) if explicit else Path(f"/tmp/x509up_u{os.getuid()}")  # noqa: S108


async def _cert_end(path: Path) -> float | None:
    openssl = shutil.which("openssl")
    if openssl is None or not path.is_file():
        return None
    result = await run_command(
        [openssl, "x509", "-noout", "-enddate", "-in", str(path)], timeout_seconds=10
    )
    match = re.search(rb"notAfter=(.+)", result.stdout)
    if not match:
        return None
    try:
        return time.mktime(time.strptime(match.group(1).decode().strip(), "%b %d %H:%M:%S %Y %Z"))
    except ValueError:
        return None


async def check_grid(settings: Settings) -> Check:
    proxy = _proxy_path()
    end = await _cert_end(proxy)
    if end and end > time.time():
        hours = (end - time.time()) / 3600
        configured = settings.grid_cert_file == proxy
        hint = "" if configured else f"; set CMSMCP_GRID_CERT_FILE/KEY_FILE={proxy}"
        return Check(OK, f"VOMS proxy valid {hours:.0f}h{hint}")
    cert, key = settings.grid_cert_file, settings.grid_key_file
    if cert.is_file() and key.is_file():
        cert_end = await _cert_end(cert)
        if cert_end and cert_end < time.time():
            return Check(FAIL, f"user certificate {cert} has expired: renew at ca.cern.ch")
        return Check(
            WARN, "certificate found, no valid proxy: run `voms-proxy-init -voms cms -rfc`"
        )
    return Check(MISSING, "no grid certificate: get one at https://ca.cern.ch (optional)")


def check_token(settings: Settings, spec: TokenSpec) -> Check:
    value = getattr(settings, spec.field)
    path = secrets_dir() / spec.field
    if path.exists() and path.stat().st_mode & 0o077:
        return Check(FAIL, f"{path} is readable by others: chmod 600 it")
    if not value:
        return Check(MISSING, f"not set (optional): `cmsmcp setup {spec.name}`")
    source = "secret file" if path.exists() else "environment"
    return Check(OK, f"token set ({source})")


def check_actions(settings: Settings) -> Check:
    personal = ", ".join(settings.personal_sources) or "none"
    if settings.actions_enabled:
        return Check(OK, f"enabled, confirm={settings.confirm_mode}; personal: {personal}")
    return Check(OK, f"disabled (read-only); personal: {personal}")


# ----------------------------------------------------------------- live checks
async def check_lxplus(settings: Settings) -> Check:
    if settings.ssh_executable is None:
        return Check(MISSING, "ssh not installed")
    target = f"{settings.lxplus_user}@{settings.lxplus_host}" if settings.lxplus_user else None
    if target is None:
        return Check(MISSING, "CMSMCP_LXPLUS_USER not set: `cmsmcp setup lxplus`")
    result = await run_command(
        [settings.ssh_executable, *ssh_options(settings), target, "echo", "cmsmcp-ok"],
        timeout_seconds=30,
    )
    if result.exit_code == 0 and b"cmsmcp-ok" in result.stdout:
        return Check(OK, f"Kerberos SSH to {target} works")
    err = result.stderr.decode(errors="replace").strip().splitlines()
    return Check(FAIL, (err[-1] if err else "ssh failed") + " (kinit first?)")


def ssh_options(settings: Settings) -> list[str]:
    """Non-interactive, Kerberos-only SSH with a shared control connection."""
    settings.ensure_directories()
    control = settings.state_dir / "ssh-%C"
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "GSSAPIAuthentication=yes",
        "-o",
        "GSSAPIDelegateCredentials=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control}",
        "-o",
        "ControlPersist=10m",
    ]


Checker = Callable[[Settings], Awaitable[Check]]


async def gather_status(settings: Settings, *, live: bool) -> list[tuple[str, Check]]:
    """Status of every credential; ``live`` adds network checks."""
    rows: list[tuple[str, Check]] = [("sso", check_sso(settings))]
    kerberos, grid = await asyncio.gather(check_kerberos(settings), check_grid(settings))
    rows += [("kerberos", kerberos), ("grid", grid)]
    if live:
        rows.append(("lxplus", await check_lxplus(settings)))
    elif settings.lxplus_user:
        rows.append(("lxplus", Check(WARN, f"{settings.lxplus_user}: not tested (`doctor`)")))
    else:
        rows.append(("lxplus", Check(MISSING, "CMSMCP_LXPLUS_USER not set")))
    rows += [(spec.name, check_token(settings, spec)) for spec in TOKENS]
    rows.append(("actions", check_actions(settings)))
    return rows


def format_status(rows: list[tuple[str, Check]]) -> str:
    width = max(len(name) for name, _ in rows)
    return "\n".join(f"  {name:<{width}}  {c.state:<7} {c.detail}" for name, c in rows)
