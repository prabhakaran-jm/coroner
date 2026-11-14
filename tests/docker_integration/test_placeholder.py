"""Placeholder Docker integration test (task 1.3).

Runs only with --run-docker or CORONER_RUN_DOCKER=1. It is replaced by the real
Experiment A, B and C integration tests in M0.7.
"""
import pytest

pytestmark = pytest.mark.docker_integration


def test_placeholder_runs_only_when_opted_in() -> None:
    assert True