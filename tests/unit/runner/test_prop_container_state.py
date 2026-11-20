"""Property 21: Container state evaluation.

**Validates: Requirements 9.6, 9.10, 9.11, 10.4, 10.6, 10.9, 11.5, 11.6**

The oracle is a literal table of accepted (exit_code, oom_killed, status) triples. It does
not use the production expected-state mapping or comparison code.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from coroner.runner.evaluate import ContainerState, Evaluation, evaluate_container_state

ACCEPTED = {
    "A": {(1, False, "exited")},
    "B": {(137, False, "exited")},
    "C": {(137, True, "exited")},
}


def oracle(experiment, code, oom, status):
    for want in ACCEPTED[experiment]:
        if (
            type(code) is int and code == want[0]
            and type(oom) is bool and oom == want[1]
            and type(status) is str and status == want[2]
        ):
            return True
    return False


exit_codes = st.one_of(
    st.sampled_from([0, 1, 2, 125, 126, 127, 128, 129, 136, 137, 138, 139, 143, 255, -1, -137]),
    st.integers(-300, 300),
)
statuses = st.sampled_from(
    ["exited", "running", "created", "restarting", "paused", "dead", "removing", "", "Exited", "EXITED", " exited", "exited "]
)


@settings(max_examples=500, deadline=None)
@given(exp=st.sampled_from("ABC"), code=exit_codes, oom=st.booleans(), status=statuses)
def test_pass_iff_expected_state(exp, code, oom, status):
    result = evaluate_container_state(exp, ContainerState(code, oom, status))
    assert isinstance(result, Evaluation)
    assert result.passed is oracle(exp, code, oom, status)
    if not result.passed:
        assert str(code) in result.message
        assert repr(status) in result.message
        assert str(oom) in result.message
        assert f"Experiment {exp}" in result.message


@settings(max_examples=200, deadline=None)
@given(
    exp=st.sampled_from("ABC"),
    code=st.one_of(st.none(), st.booleans(), st.floats(allow_nan=True), st.text(max_size=5), st.just("137"), st.just(137.0)),
    oom=st.one_of(st.none(), st.integers(-2, 2), st.sampled_from(["true", "false", "True", ""]), st.just(1.0), st.just([])),
    status=st.one_of(st.none(), st.integers(), st.just(b"exited"), st.just(("exited",))),
)
def test_non_exact_types_never_pass(exp, code, oom, status):
    result = evaluate_container_state(exp, ContainerState(code, oom, status))
    assert result.passed is False


@pytest.mark.parametrize("exp, code, oom", [("A", 1, False), ("B", 137, False), ("C", 137, True)])
def test_expected_state_passes(exp, code, oom):
    result = evaluate_container_state(exp, ContainerState(code, oom, "exited"))
    assert result.passed is True
    assert all(c.passed for c in result.checks)


@pytest.mark.parametrize(
    "exp, code, oom, status",
    [
        ("A", 137, False, "exited"),
        ("A", 0, False, "exited"),
        ("A", 1, True, "exited"),
        ("A", 1, False, "running"),
        ("B", 1, False, "exited"),
        ("B", 136, False, "exited"),
        ("B", 137, True, "exited"),
        ("C", 137, False, "exited"),
        ("C", 1, True, "exited"),
        ("C", 137, True, "dead"),
    ],
)
def test_wrong_state_fails_with_observed_values(exp, code, oom, status):
    result = evaluate_container_state(exp, ContainerState(code, oom, status))
    assert result.passed is False
    assert f"exit_code={code!r}" in result.message
    assert f"oom_killed={oom!r}" in result.message
    assert f"status={status!r}" in result.message


@pytest.mark.parametrize("exp", ["A", "B", "C"])
def test_bool_is_not_an_int_exit_code(exp):
    # True == 1 and A expects 1; it must still be rejected.
    assert evaluate_container_state("A", ContainerState(True, False, "exited")).passed is False
    assert evaluate_container_state(exp, ContainerState(False, False, "exited")).passed is False


@pytest.mark.parametrize("oom", [1, 0, "true", "false", None, 1.0, "True"])
@pytest.mark.parametrize("exp, code", [("A", 1), ("B", 137), ("C", 137)])
def test_non_bool_oom_is_rejected(exp, code, oom):
    assert evaluate_container_state(exp, ContainerState(code, oom, "exited")).passed is False


@pytest.mark.parametrize("state", [None, {}, {"exit_code": 1, "oom_killed": False, "status": "exited"}, "exited", 1])
@pytest.mark.parametrize("exp", ["A", "B", "C"])
def test_missing_or_malformed_state_fails_without_raising(exp, state):
    result = evaluate_container_state(exp, state)
    assert result.passed is False
    assert "Container_State could not be read" in result.message


@pytest.mark.parametrize("exp", ["", "a", "D", "AB", "A ", "exp_a", "1", None, 1, ["A"]])
def test_invalid_experiment_raises(exp):
    with pytest.raises(ValueError) as e:
        evaluate_container_state(exp, ContainerState(1, False, "exited"))
    assert repr(exp) in str(e.value)


def test_message_on_failure_names_expected_and_observed():
    msg = evaluate_container_state("C", ContainerState(137, False, "exited")).message
    assert "oom_killed=False" in msg and "oom_killed=True" in msg
    assert "exit_code=137" in msg


# --- isolated negative controls: mutant evaluators must disagree with the oracle ---

def _truthy_mutant(exp, code, oom, status):
    want = {"A": (1, False), "B": (137, False), "C": (137, True)}[exp]
    return code == want[0] and bool(oom) == want[1] and status == "exited"


def _ignore_status_mutant(exp, code, oom, status):
    want = {"A": (1, False), "B": (137, False), "C": (137, True)}[exp]
    return code == want[0] and oom is want[1]


def _swapped_mutant(exp, code, oom, status):
    want = {"A": (1, False), "B": (137, True), "C": (137, True)}[exp]
    return code == want[0] and oom is want[1] and status == "exited"


def test_negative_control_truthy_oom_mutant_is_detected():
    assert _truthy_mutant("C", 137, 1, "exited") is True
    assert oracle("C", 137, 1, "exited") is False
    assert evaluate_container_state("C", ContainerState(137, 1, "exited")).passed is False


def test_negative_control_bool_as_int_mutant_is_detected():
    assert _truthy_mutant("A", True, False, "exited") is True
    assert oracle("A", True, False, "exited") is False
    assert evaluate_container_state("A", ContainerState(True, False, "exited")).passed is False


def test_negative_control_status_ignoring_mutant_is_detected():
    assert _ignore_status_mutant("A", 1, False, "running") is True
    assert oracle("A", 1, False, "running") is False
    assert evaluate_container_state("A", ContainerState(1, False, "running")).passed is False


def test_negative_control_swapped_expectation_mutant_is_detected():
    assert _swapped_mutant("B", 137, True, "exited") is True
    assert oracle("B", 137, True, "exited") is False
    assert evaluate_container_state("B", ContainerState(137, True, "exited")).passed is False
