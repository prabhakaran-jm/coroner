"""Docker state resolution tests (task 2.12, part 2).

Synthetic tmp_path fixtures built with RunsBuilder only. Nothing reads the real runs/ directory and no
Docker call is made.

The `finished_at` comparison rule is tested on `_compare_fields` with hand-built blocks. Reason: today
`classify._parse_raw` always sets the raw `finished_at` to None and `loader.py` never passes the raw
`State.FinishedAt` on, so a document read from `docker_inspect.json` cannot reach that comparison through
`resolve_docker_state`. This is reported to the reviewer and is not pinned by any test here.
"""
from __future__ import annotations

import time

import pytest

from coroner.runner.manifest import deserialize_manifest
from coroner.viewer.classify import (
    DockerManifestBlock,
    DockerRawBlock,
    _compare_fields,
    resolve_docker_state,
    resolve_docker_state_with_fault,
    verdict_of,
)
from coroner.viewer.files import DEFAULT_IO, open_runs_root
from coroner.viewer.loader import load_detail
from tests.unit.viewer.builder import InspectSpec, RunsBuilder, StateSpec

ID_A = "a" * 64
ID_B = "b" * 64
FIELDS = ["container_id", "status", "exit_code", "oom_killed", "memory_limit_bytes", "finished_at"]


def _build(tmp_path, *, state=None, inspect=None, **kwargs):
    """Build one run. `state`/`inspect` follow RunsBuilder semantics (omit for the matching default)."""
    builder = RunsBuilder(tmp_path / "runs")
    args = dict(kwargs)
    if state is not None:
        args["container_state"] = state
    if inspect is not None:
        args["inspect"] = inspect
    run_id = builder.add_run("B", **args)
    run_dir = tmp_path / "runs" / run_id
    manifest = deserialize_manifest((run_dir / "manifest.json").read_bytes())
    inspect_path = run_dir / "docker_inspect.json"
    raw = inspect_path.read_bytes() if inspect_path.exists() else None
    return run_id, manifest, raw


def _results(view) -> dict[str, str]:
    return {f.field: f.result for f in view.fields}


# ------------------------------------------------------------------ the five source codes


def test_source_inspect_and_manifest_agree(tmp_path) -> None:
    run_id, manifest, raw = _build(tmp_path)
    view = resolve_docker_state(manifest, raw, run_id)

    assert view.source == "inspect_and_manifest_agree"
    assert view.raw_unavailable_reason is None
    assert view.discrepancies == []


def test_source_inspect_and_manifest_disagree(tmp_path) -> None:
    run_id, manifest, raw = _build(tmp_path, state=StateSpec(exit_code=137), inspect=InspectSpec(exit_code=1))
    view = resolve_docker_state(manifest, raw, run_id)

    assert view.source == "inspect_and_manifest_disagree"
    assert [d.field for d in view.discrepancies] == ["exit_code"]


def test_source_manifest_only_when_raw_is_missing(tmp_path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run("B", inspect=None)
    manifest = deserialize_manifest((tmp_path / "runs" / run_id / "manifest.json").read_bytes())

    view = resolve_docker_state(manifest, None, run_id)

    assert view.source == "manifest_only"
    assert view.raw is None
    assert view.raw_unavailable_reason == "missing"


def test_source_raw_only_when_manifest_has_no_container_state(tmp_path) -> None:
    run_id, manifest, raw = _build(tmp_path, state=None, inspect=InspectSpec(), container_state=None)
    assert manifest.container_state is None

    view = resolve_docker_state(manifest, raw, run_id)

    assert view.source == "raw_only"
    assert view.raw.exit_code == 137
    assert view.effective.exit_code == 137


def test_source_unavailable_when_neither_source_exists(tmp_path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run("B", container_state=None, inspect=None)
    manifest = deserialize_manifest((tmp_path / "runs" / run_id / "manifest.json").read_bytes())

    view = resolve_docker_state(manifest, None, run_id)

    assert view.source == "unavailable"
    assert view.effective.exit_code is None
    assert view.effective.oom_killed is None
    assert view.effective.memory_limit_bytes is None
    assert view.effective.memory_limit_kind is None
    assert view.fields == []


# ------------------------------------------------------------------ one discrepant field at a time


@pytest.mark.parametrize(
    ("field", "state", "inspect"),
    [
        ("container_id", StateSpec(container_id=ID_A), InspectSpec(container_id=ID_B)),
        ("status", StateSpec(status="exited"), InspectSpec(status="dead")),
        ("exit_code", StateSpec(exit_code=137), InspectSpec(exit_code=1)),
        ("oom_killed", StateSpec(oom_killed=False), InspectSpec(oom_killed=True)),
        ("memory_limit_bytes", StateSpec(memory=134_217_728), InspectSpec(memory=0)),
    ],
)
def test_each_field_discrepant_on_its_own(tmp_path, field: str, state: StateSpec, inspect: InspectSpec) -> None:
    run_id, manifest, raw = _build(tmp_path, state=state, inspect=inspect)
    view = resolve_docker_state(manifest, raw, run_id)

    assert view.source == "inspect_and_manifest_disagree"
    assert [d.field for d in view.discrepancies] == [field]
    results = _results(view)
    assert results[field] == "discrepancy"
    for other in FIELDS:
        if other != field:
            assert results[other] != "discrepancy", other


def test_finished_at_discrepant_on_its_own() -> None:
    manifest_block = DockerManifestBlock(present=True, finished_at="2026-11-20T21:13:21.760Z")
    raw_block = DockerRawBlock(finished_at="2026-11-20T21:13:21.761000000Z")

    by_field = {f.field: f.result for f in _compare_fields(manifest_block, raw_block)}

    assert by_field["finished_at"] == "discrepancy"


def test_all_six_fields_are_compared(tmp_path) -> None:
    run_id, manifest, raw = _build(tmp_path)
    view = resolve_docker_state(manifest, raw, run_id)

    assert [f.field for f in view.fields] == FIELDS


# ------------------------------------------------------------------ FinishedAt rules


@pytest.mark.parametrize(
    "raw_text",
    [
        "2026-11-20T21:13:21.760545826Z",  # the shape in the real runs (nine digits)
        "2026-11-20T21:13:21.760000000Z",
        "2026-11-20T21:13:21.760999999Z",  # rounds DOWN to .760, never up to .761
        "2026-11-20T21:13:21.76Z",
    ],
)
def test_nine_digit_finished_at_rounds_down_to_the_manifest_millisecond_value(raw_text: str) -> None:
    manifest_block = DockerManifestBlock(present=True, finished_at="2026-11-20T21:13:21.760Z")

    by_field = {f.field: f for f in _compare_fields(manifest_block, DockerRawBlock(finished_at=raw_text))}

    assert by_field["finished_at"].result == "equal"


def test_finished_at_one_millisecond_apart_is_a_discrepancy() -> None:
    manifest_block = DockerManifestBlock(present=True, finished_at="2026-11-20T21:13:21.760Z")
    raw_block = DockerRawBlock(finished_at="2026-11-20T21:13:21.759999999Z")

    assert {f.field: f.result for f in _compare_fields(manifest_block, raw_block)}["finished_at"] == "discrepancy"


def test_zero_date_is_not_recorded_and_not_a_discrepancy() -> None:
    zero = "0001-01-01T00:00:00Z"

    only_manifest = {
        f.field: f.result
        for f in _compare_fields(DockerManifestBlock(present=True, finished_at="2026-11-20T21:13:21.760Z"),
                                 DockerRawBlock(finished_at=zero))
    }
    neither = {
        f.field: f.result
        for f in _compare_fields(DockerManifestBlock(present=True), DockerRawBlock(finished_at=zero))
    }

    assert only_manifest["finished_at"] == "not_compared_raw_missing"
    assert neither["finished_at"] == "not_recorded_both"


def test_unparseable_finished_at_is_not_recorded() -> None:
    results = {
        f.field: f.result
        for f in _compare_fields(DockerManifestBlock(present=True, finished_at="2026-11-20T21:13:21.760Z"),
                                 DockerRawBlock(finished_at="not a time"))
    }

    assert results["finished_at"] == "not_compared_raw_missing"


# ------------------------------------------------------------------ one source only


def test_a_field_recorded_in_one_source_only_is_never_a_discrepancy(tmp_path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run("B", inspect=None)
    manifest = deserialize_manifest((tmp_path / "runs" / run_id / "manifest.json").read_bytes())
    manifest_only = resolve_docker_state(manifest, None, run_id)

    run_id2, manifest2, raw2 = _build(tmp_path / "second", container_state=None, inspect=InspectSpec())
    raw_only = resolve_docker_state(manifest2, raw2, run_id2)

    for field in ("container_id", "status", "exit_code", "oom_killed", "memory_limit_bytes"):
        assert _results(manifest_only)[field] == "not_compared_raw_missing", field
        assert _results(raw_only)[field] == "not_compared_manifest_missing", field
    assert manifest_only.discrepancies == []
    assert raw_only.discrepancies == []


# ------------------------------------------------------------------ raw unavailable reasons


@pytest.mark.parametrize(
    ("raw_bytes", "reason"),
    [
        (b"{this is not json", "invalid_json"),
        (b"", "invalid_json"),
        (b"{}", "rejected"),
        (b"[]", "rejected"),
        (b"[1]", "rejected"),
        (b'[{"Id": "short"}]', "rejected"),
    ],
)
def test_raw_malformed_or_rejected(tmp_path, raw_bytes: bytes, reason: str) -> None:
    run_id, manifest, _ = _build(tmp_path)

    view = resolve_docker_state(manifest, raw_bytes, run_id)

    assert view.source == "manifest_only"
    assert view.raw is None
    assert view.raw_unavailable_reason == reason


@pytest.mark.parametrize("fault", ["missing", "unreadable", "too_large"])
def test_raw_missing_unreadable_and_oversized(tmp_path, fault: str) -> None:
    run_id, manifest, _ = _build(tmp_path)

    view = resolve_docker_state_with_fault(manifest, None, run_id, raw_fault=fault)

    assert view.source == "manifest_only"
    assert view.raw is None
    assert view.raw_unavailable_reason == fault
    # The manifest values still come back as the effective values.
    assert view.effective.exit_code == 137


# ------------------------------------------------------------------ memory limit kind


@pytest.mark.parametrize(
    ("memory", "kind"),
    [(0, "none_configured"), (134_217_728, "bytes"), (1, "bytes")],
)
def test_memory_limit_kind(tmp_path, memory: int, kind: str) -> None:
    run_id, manifest, raw = _build(tmp_path, state=StateSpec(memory=memory), inspect=InspectSpec(memory=memory))

    view = resolve_docker_state(manifest, raw, run_id)

    assert view.effective.memory_limit_bytes == memory
    assert view.effective.memory_limit_kind == kind


def test_memory_limit_not_recorded_has_no_kind(tmp_path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run("B", container_state=None, inspect=None)
    manifest = deserialize_manifest((tmp_path / "runs" / run_id / "manifest.json").read_bytes())

    view = resolve_docker_state(manifest, None, run_id)

    assert view.effective.memory_limit_bytes is None
    assert view.effective.memory_limit_kind is None


# ------------------------------------------------------------------ verdict and run id


@pytest.mark.parametrize(("outcome", "verdict"), [("passed", "PASS"), ("failed", "FAIL"), ("aborted", "UNKNOWN")])
def test_verdict_comes_from_the_stored_outcome_during_a_discrepancy(tmp_path, outcome: str, verdict: str) -> None:
    run_id, manifest, raw = _build(
        tmp_path, outcome=outcome, state=StateSpec(exit_code=137), inspect=InspectSpec(exit_code=1)
    )

    view = resolve_docker_state(manifest, raw, run_id)

    assert view.source == "inspect_and_manifest_disagree"
    assert verdict_of(manifest.outcome) == verdict


def test_manifest_run_id_mismatch_makes_the_manifest_invalid(tmp_path) -> None:
    """R-A: a manifest whose run_id differs from the directory name is unusable (reason run_id_mismatch)."""
    donor = RunsBuilder(tmp_path / "donor")
    donor_id = donor.add_run("B", run_id="donor-run-00000001")
    donor_manifest = (tmp_path / "donor" / donor_id / "manifest.json").read_bytes()

    runs = RunsBuilder(tmp_path / "runs")
    run_id = runs.add_run("B", run_id="actual-dir-00000001", manifest_bytes=donor_manifest)

    record = load_detail(open_runs_root(tmp_path / "runs"), run_id, DEFAULT_IO, time.time_ns)

    assert record.manifest is None
    assert record.manifest_fault.status == "invalid"
    assert record.manifest_fault.reason == "run_id_mismatch"
    assert record.verdict == "UNKNOWN"
    # Rule: the other two artifacts are not read when the manifest is unusable.
    assert record.evidence_fault.status == "not_read"
    assert record.inspect_fault.status == "not_read"
