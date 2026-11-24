"""Classification and verdict tests (task 2.12, part 1).

Covers experiment_type, verdict_of, and the rule that ExitCode and OOMKilled are two independent recorded
facts: experiment B (137, OOMKilled false) and experiment C (137, OOMKilled true) are returned as recorded,
never inferred from each other. Every fixture lives in tmp_path; nothing reads the real runs/ directory.
"""
from __future__ import annotations

import pytest

from coroner.runner.manifest import deserialize_manifest
from coroner.viewer.classify import experiment_type, resolve_docker_state, verdict_of

from tests.unit.viewer.builder import InspectSpec, RunsBuilder, StateSpec


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("A", "A"), ("B", "B"), ("C", "C"),
        ("a", "A"), ("b", "B"), ("c", "C"),
    ],
)
def test_experiment_type_accepts_a_b_c_in_either_case(value: str, expected: str) -> None:
    assert experiment_type(value) == expected


@pytest.mark.parametrize("value", ["x", "X", "D", "", "AB", " A", "A ", "a\n", "experiment-a", None, 1, 0, 1.5, True, ["A"]])
def test_experiment_type_returns_none_for_everything_else(value: object) -> None:
    assert experiment_type(value) is None


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("passed", "PASS"),
        ("failed", "FAIL"),
        ("aborted", "UNKNOWN"),
        ("PASSED", "UNKNOWN"),
        ("", "UNKNOWN"),
        ("ok", "UNKNOWN"),
        (None, "UNKNOWN"),
        (1, "UNKNOWN"),
        (True, "UNKNOWN"),
    ],
)
def test_verdict_of_depends_only_on_the_stored_outcome(outcome: object, expected: str) -> None:
    assert verdict_of(outcome) == expected


def _docker_view(tmp_path, experiment: str, exit_code: int, oom_killed: bool):
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run(
        experiment,
        container_state=StateSpec(exit_code=exit_code, oom_killed=oom_killed),
        inspect=InspectSpec(exit_code=exit_code, oom_killed=oom_killed),
    )
    run_dir = tmp_path / "runs" / run_id
    manifest = deserialize_manifest((run_dir / "manifest.json").read_bytes())
    view = resolve_docker_state(manifest, (run_dir / "docker_inspect.json").read_bytes(), run_id)
    return manifest, view


@pytest.mark.parametrize(
    ("experiment", "exit_code", "oom_killed"),
    [
        ("B", 137, False),  # Experiment B: SIGKILL, not an OOM kill
        ("C", 137, True),   # Experiment C: same exit code, OOMKilled true
        ("A", 1, False),
        # The cross pairs show that experiment type, ExitCode and OOMKilled never determine each other.
        ("B", 137, True),
        ("C", 137, False),
        ("A", 137, True),
        ("B", 1, False),
        ("C", 0, True),
    ],
)
def test_exit_code_and_oom_killed_are_returned_as_recorded(tmp_path, experiment: str, exit_code: int, oom_killed: bool) -> None:
    manifest, view = _docker_view(tmp_path, experiment, exit_code, oom_killed)

    # Recorded values come back unchanged from both sources and from the effective block.
    assert view.manifest.exit_code == exit_code
    assert view.manifest.oom_killed is oom_killed
    assert view.raw.exit_code == exit_code
    assert view.raw.oom_killed is oom_killed
    assert view.effective.exit_code == exit_code
    assert view.effective.oom_killed is oom_killed
    assert view.source == "inspect_and_manifest_agree"
    # The type comes only from the manifest `experiment` string.
    assert experiment_type(manifest.experiment) == experiment


def test_b_and_c_share_exit_code_137_and_differ_only_in_recorded_oom_killed(tmp_path) -> None:
    _, view_b = _docker_view(tmp_path / "b", "B", 137, False)
    _, view_c = _docker_view(tmp_path / "c", "C", 137, True)

    assert view_b.effective.exit_code == view_c.effective.exit_code == 137
    assert view_b.effective.oom_killed is False
    assert view_c.effective.oom_killed is True


@pytest.mark.parametrize("oom_killed", [False, True])
def test_changing_oom_killed_never_changes_exit_code_or_experiment_type(tmp_path, oom_killed: bool) -> None:
    manifest, view = _docker_view(tmp_path, "B", 137, oom_killed)

    assert view.effective.exit_code == 137
    assert experiment_type(manifest.experiment) == "B"


@pytest.mark.parametrize("exit_code", [0, 1, 137, 143])
def test_changing_exit_code_never_changes_oom_killed_or_experiment_type(tmp_path, exit_code: int) -> None:
    manifest, view = _docker_view(tmp_path, "C", exit_code, True)

    assert view.effective.oom_killed is True
    assert experiment_type(manifest.experiment) == "C"


@pytest.mark.parametrize("outcome", ["passed", "failed", "aborted"])
def test_verdict_ignores_exit_code_and_oom_killed(tmp_path, outcome: str) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run("B", outcome=outcome, container_state=StateSpec(exit_code=137, oom_killed=True))
    manifest = deserialize_manifest((tmp_path / "runs" / run_id / "manifest.json").read_bytes())

    expected = {"passed": "PASS", "failed": "FAIL", "aborted": "UNKNOWN"}[outcome]
    assert verdict_of(manifest.outcome) == expected
