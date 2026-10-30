"""Hatchling build hook: bundle the pinned obscura browser binary into the wheel.

CMcpS uses obscura (https://github.com/h4ckf0r0day/obscura, Apache-2.0) as its
headless browser, driven by Playwright over CDP. The wheel embeds the release
binary for the *build platform* so a released package needs no system browser
for headless work; the visible SSO sign-in still uses a system Chromium or
Firefox. The download is pinned by version and verified against the sha256
digest published on the GitHub release. Set ``CMCPS_SKIP_OBSCURA_BUNDLE=1``
to build a wheel without the bundled binary (source checkouts and offline
builds then fall back to the system browser for headless work too).
"""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

OBSCURA_VERSION = "v0.2.5"
_BUNDLED_DIR = Path("src") / "cmcps" / "_obscura"

# (sys.platform, platform.machine()) -> (release asset name, sha256 digest
# as published on the GitHub release). Rendering variant, non-stealth: the
# stealth transport is meant for bot-detection evasion, not for CERN SSO.
_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("linux", "x86_64"): (
        "obscura-x86_64-linux.tar.gz",
        "2116764b99bc084fae3668b68aa535b4cf456e4498845b47493ad4123e83ca36",
    ),
    ("linux", "aarch64"): (
        "obscura-aarch64-linux.tar.gz",
        "24c549a348b1d464e80aa3f643bc0c874b0a2942fb7836c683528028d0fbb190",
    ),
    ("darwin", "arm64"): (
        "obscura-aarch64-macos.tar.gz",
        "fcca31eba4280cd67a2e226ef4b015e432d5c089f78cefd074ad12bbc8e23345",
    ),
    ("darwin", "x86_64"): (
        "obscura-x86_64-macos.tar.gz",
        "9f354cf929a577491d6ba0ef451e07baefffd19d34115330f69a0e463771d6cd",
    ),
}

_RELEASE_URL = (
    f"https://github.com/h4ckf0r0day/obscura/releases/download/{OBSCURA_VERSION}/{{asset}}"
)


class BuildHook(BuildHookInterface):
    """Fetch, verify, and lay down the obscura binary as wheel package data."""

    def initialize(self, version: str, build_data: dict) -> None:  # noqa: ARG002
        if self.target_name != "wheel":
            return
        if os.environ.get("CMCPS_SKIP_OBSCURA_BUNDLE") == "1":
            self.app.display_warning("obscura bundling skipped (CMCPS_SKIP_OBSCURA_BUNDLE=1)")
            return
        key = (sys.platform, platform.machine())
        asset = _ASSETS.get(key)
        if asset is None:
            self.app.display_warning(
                f"no pinned obscura release for {key[0]}-{key[1]}; "
                "the wheel will fall back to a system browser for headless work",
            )
            return
        name, expected_sha256 = asset
        url = _RELEASE_URL.format(asset=name)
        self.app.display_info(f"downloading {url}")
        archive = _download(url, expected_sha256)
        destination = Path(self.root) / _BUNDLED_DIR
        destination.mkdir(parents=True, exist_ok=True)
        binary_path = _extract_obscura(archive, destination)
        binary_path.chmod(binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        # The runtime refuses a binary built for another platform.
        (destination / "PLATFORM").write_text(f"{sys.platform}-{platform.machine()}\n")
        self.app.display_info(f"bundled obscura {OBSCURA_VERSION} as {binary_path}")


def _download(url: str, expected_sha256: str) -> bytes:
    with urllib.request.urlopen(url, timeout=600) as response:  # noqa: S310 - pinned release URL
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise RuntimeError(
            f"obscura release checksum mismatch: expected {expected_sha256}, got {digest}",
        )
    return payload


def _extract_obscura(archive: bytes, destination: Path) -> Path:
    """Extract only the ``obscura`` binary (the worker is used by scrape, not us)."""
    target = destination / "obscura"
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as handle:
        handle.write(archive)
        handle.flush()
        with tarfile.open(handle.name, "r:gz") as tar:
            member = next(
                (
                    m
                    for m in tar.getmembers()
                    if m.isfile() and m.name.rsplit("/", 1)[-1] == "obscura"
                ),
                None,
            )
            if member is None:
                raise RuntimeError("obscura binary not found in the release archive")
            extracted = tar.extractfile(member)
            if extracted is None:
                raise RuntimeError("obscura binary could not be read from the release archive")
            target.write_bytes(extracted.read())
    return target
