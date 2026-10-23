"""Shared CMcpS test fixtures."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from cmcps.config.settings import Settings


@pytest.fixture
def future_epoch() -> float:
    return time.time() + 3600


@pytest.fixture
def valid_storage_state(future_epoch: float) -> dict[str, Any]:
    return {
        "cookies": [
            {
                "name": "KEYCLOAK_IDENTITY",
                "value": "sso-secret-value",
                "domain": ".auth.cern.ch",
                "path": "/",
                "expires": future_epoch,
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            },
            {
                "name": "SERVICE",
                "value": "service-secret-value",
                "domain": ".service.cern.ch",
                "path": "/",
                "expires": future_epoch,
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            },
        ],
        "origins": [],
    }


@pytest.fixture
def expired_storage_state() -> dict[str, Any]:
    return {
        "cookies": [
            {
                "name": "KEYCLOAK_IDENTITY",
                "value": "expired-secret",
                "domain": ".auth.cern.ch",
                "path": "/",
                "expires": time.time() - 60,
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(state_dir=tmp_path, browser_executable=None)
