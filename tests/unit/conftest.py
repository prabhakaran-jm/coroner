"""Unit-test guards: no network and no Docker CLI (Requirement 13.8)."""
from __future__ import annotations

import pytest

from tests.unit import guards


@pytest.fixture(autouse=True)
def _no_network_no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    guards.install(monkeypatch)