"""Pure evaluation for the runner, part 1 (Requirements 8.4, 9.6, 9.10, 9.11, 10.4, 10.6,
10.9, 11.5, 11.6).

Holds `parse_mem_size` (re-exported from `coroner.common.envparse`), `ContainerState`,
`Evaluation`, and `evaluate_container_state`. No I/O, no Docker, no clock.
"""
from __future__ import annotations

from dataclasses import dataclass

from coroner.common.envparse import parse_mem_size

__all__ = [
    "Check",
    "ContainerState",
    "EXPECTED_STATE",
    "EXPECTED_STATUS",
    "Evaluation",
    "evaluate_container_state",
    "parse_mem_size",
]

# experiment -> (exit_code, oom_killed)
EXPECTED_STATE: dict[str, tuple[int, bool]] = {
    "A": (1, False),
    "B": (137, False),
    "C": (137, True),
}
EXPECTED_STATUS = "exited"


@dataclass(frozen=True)
class ContainerState:
    """Observed Docker `State`: `ExitCode`, `OOMKilled`, `Status`."""

    exit_code: int
    oom_killed: bool
    status: str


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str | None = None


@dataclass(frozen=True)
class Evaluation:
    passed: bool
    message: str
    checks: tuple[Check, ...] = ()


def _observed(state: ContainerState) -> str:
    return (
        f"exit_code={state.exit_code!r} oom_killed={state.oom_killed!r} status={state.status!r}"
    )


def evaluate_container_state(experiment: str, state: ContainerState) -> Evaluation:
    """Pass iff the observed state equals the experiment's expected state.

    Expected: A (1, False), B (137, False), C (137, True), status "exited". Types are
    exact: `exit_code` must be an int and not a bool, `oom_killed` must be a bool, and
    `status` must be the str "exited". A missing or malformed state fails (never raises).
    An unknown experiment identifier raises ValueError naming the value.
    """
    if not isinstance(experiment, str) or experiment not in EXPECTED_STATE:
        raise ValueError(f"unknown experiment {experiment!r} (expected one of A, B, C)")
    want_code, want_oom = EXPECTED_STATE[experiment]
    expected = f"exit_code={want_code!r} oom_killed={want_oom!r} status={EXPECTED_STATUS!r}"
    if not isinstance(state, ContainerState):
        return Evaluation(
            False,
            f"Experiment {experiment}: Container_State could not be read "
            f"(got {state!r}); expected {expected}",
        )
    code, oom, status = state.exit_code, state.oom_killed, state.status
    checks = (
        Check("exit_code", type(code) is int and code == want_code, f"observed {code!r}, expected {want_code!r}"),
        Check("oom_killed", type(oom) is bool and oom is want_oom, f"observed {oom!r}, expected {want_oom!r}"),
        Check("status", type(status) is str and status == EXPECTED_STATUS, f"observed {status!r}, expected {EXPECTED_STATUS!r}"),
    )
    if all(c.passed for c in checks):
        return Evaluation(True, f"Experiment {experiment}: observed {_observed(state)}", checks)
    return Evaluation(
        False,
        f"Experiment {experiment}: unexpected container state, observed {_observed(state)}; "
        f"expected {expected}",
        checks,
    )
