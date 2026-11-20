"""Opt-in control for Docker integration tests.

Default `pytest` runs only Docker-free, network-free unit tests (Requirement 13.10).
Docker integration tests run only with `--run-docker` or CORONER_RUN_DOCKER=1
(Requirement 13.11). When opted in and Docker is unavailable they are skipped with
the reason "Docker is unavailable: ..." (Requirement 13.6).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MARKER = "docker_integration"
ENV_VAR = "CORONER_RUN_DOCKER"
OPT_IN_HINT = "opt in with --run-docker or CORONER_RUN_DOCKER=1"

_INTEGRATION_DIR = Path(__file__).resolve().parent / "docker_integration"
_DOCKER_INFO_TIMEOUT_S = 30


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-docker",
        action="store_true",
        default=False,
        dest="run_docker",
        help="Run the Docker integration tests (marker: docker_integration). "
        "Same as CORONER_RUN_DOCKER=1.",
    )


def is_opted_in(config: pytest.Config) -> bool:
    """True only for the --run-docker flag or the exact value CORONER_RUN_DOCKER=1."""
    return bool(config.getoption("run_docker")) or os.environ.get(ENV_VAR) == "1"


def docker_unavailable_reason() -> str | None:
    """Return why Docker cannot be used, or None when `docker info` succeeds."""
    exe = shutil.which("docker")
    if exe is None:
        return "the docker executable was not found on PATH"
    try:
        proc = subprocess.run(
            [exe, "info"], capture_output=True, text=True, timeout=_DOCKER_INFO_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"`docker info` failed: {exc}"
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = lines[0] if lines else "no output"
        return f"`docker info` exited with {proc.returncode}: {detail}"
    return None


def _under_integration_dir(item: pytest.Item) -> bool:
    return _INTEGRATION_DIR in Path(str(item.path)).resolve().parents


def pytest_report_header(config: pytest.Config) -> str:
    if is_opted_in(config):
        return f"coroner: {MARKER} tests enabled (opted in)"
    return f"coroner: {MARKER} tests are deselected ({OPT_IN_HINT})"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    integration: list[pytest.Item] = []
    for item in items:
        # Everything in tests/docker_integration is an integration test, even if the
        # author forgot the marker, so it can never run in the default invocation.
        if _under_integration_dir(item) and item.get_closest_marker(MARKER) is None:
            item.add_marker(getattr(pytest.mark, MARKER))
        if item.get_closest_marker(MARKER) is not None:
            integration.append(item)
    if not integration:
        return

    if not is_opted_in(config):
        config.hook.pytest_deselected(items=integration)
        drop = {id(item) for item in integration}
        items[:] = [item for item in items if id(item) not in drop]
        return

    # Seam for tests of this module: a plugin may set config._coroner_docker_probe.
    probe = getattr(config, "_coroner_docker_probe", docker_unavailable_reason)
    reason = probe()
    if reason is not None:
        skip = pytest.mark.skip(reason=f"Docker is unavailable: {reason}")
        for item in integration:
            item.add_marker(skip)