"""Project isolation gate shared by the live Docker integration modules (pure functions, no Docker, no I/O).

Every live module selects its Compose project from an environment variable. There is NO default project: when the
variable is missing or unsafe, the first statement of every live fixture (`gate(VAR)`) fails the test through
`pytest.fail` before any Docker command or container creation.

    M0.5 modules  test_compose_isolation.py, test_volume_persistence.py, test_collector_readonly_rootfs.py
                  variable CORONER_TEST_M05_PROJECT      e.g. $env:CORONER_TEST_M05_PROJECT = 'coroner-t83-m05'
    M0.7 modules  test_ordinary_500.py, test_experiment_a.py, test_experiment_b.py, test_experiment_c.py
                  variable CORONER_TEST_PROJECT          e.g. $env:CORONER_TEST_PROJECT = 'coroner-t83'

The evidence volume of a selected project is always `<project>_coroner_evidence`. The projects `coroner`,
`coroner-m07` and `coroner-spike` and their volumes (PROTECTED_VOLUMES) are never selectable.

LIFECYCLE RULE (modules run strictly sequentially, one at a time, each separately with --run-docker):
the persistence module stops its Collector but leaves a STOPPED container behind. Before the read-only-rootfs module
the coordinator must run, by hand,
    docker compose -p coroner-t83-m05 -f compose.yaml down
(never `-v`, never a prune, never a volume removal). The read-only-rootfs module fails closed while any container
still uses the volume. The M0.7 modules tear their own stack down with `down` (no `-v`) unless CORONER_KEEP_STACK=1.
"""
from __future__ import annotations

import os

import pytest

from coroner.runner.docker_port import validate_compose_project

M05_VAR = "CORONER_TEST_M05_PROJECT"
M07_VAR = "CORONER_TEST_PROJECT"
EXAMPLE_PROJECTS = {M05_VAR: "coroner-t83-m05", M07_VAR: "coroner-t83"}

PROTECTED_VOLUMES = (
    "coroner_coroner_evidence",
    "coroner-spike_coroner_spike_evidence",
    "coroner-m07_coroner_evidence",
)
FORBIDDEN_PROJECTS = ("coroner", "coroner-m07", "coroner-spike")
EVIDENCE_VOLUME_SUFFIX = "_coroner_evidence"


def selected_project(var: str) -> str:
    """The raw value of `var`, or '' when unset. Never a default, never raises."""
    return os.environ.get(var, "")


def evidence_volume(project: str) -> str:
    """Evidence volume of `project` (`<project>_coroner_evidence`); '' for an empty project."""
    return f"{project}{EVIDENCE_VOLUME_SUFFIX}" if project else ""


def validate_selected_project(var: str, value: str) -> str:
    """Return `value` when it is an acceptable isolated project name; otherwise raise ValueError."""
    example = EXAMPLE_PROJECTS.get(var, "coroner-t83")
    hint = f"Set it to an isolated project, e.g. $env:{var} = '{example}'."
    if not value:
        raise ValueError(f"{var} is not set (live Docker tests have no default project). {hint}")
    try:
        validate_compose_project(value)
    except ValueError as exc:
        raise ValueError(f"{var}={value!r} is not a valid Compose project name ({exc}). {hint}") from None
    if value in FORBIDDEN_PROJECTS:
        raise ValueError(f"{var}={value!r} is a protected project (forbidden: {', '.join(FORBIDDEN_PROJECTS)}). {hint}")
    if evidence_volume(value) in PROTECTED_VOLUMES:
        raise ValueError(f"{var}={value!r} maps to the protected volume {evidence_volume(value)}. {hint}")
    return value


def gate(var: str) -> str:
    """Fail the test (pytest.fail) BEFORE any Docker call when the project selected by `var` is missing or unsafe."""
    try:
        return validate_selected_project(var, selected_project(var))
    except ValueError as exc:
        pytest.fail(f"live Docker test refused: {exc}", pytrace=False)


def runner_env(base: dict[str, str], project: str) -> dict[str, str]:
    """Copy of `base` for the Runner subprocess with CORONER_COMPOSE_PROJECT set to the selected project."""
    env = dict(base)
    env["CORONER_COMPOSE_PROJECT"] = project
    return env
