"""End-to-end regression tests for load_detail + to_detail + dump_json.

Synthetic runs in tmp_path only (RunsBuilder); nothing reads the real runs/ directory.
Covers two defects: to_detail failing for any run with Samples (x-axis tick serializer), and the raw
State.FinishedAt never reaching the Docker comparison.
"""
from __future__ import annotations

import json
import time

import pytest

from coroner.viewer.files import DEFAULT_IO, open_runs_root
from coroner.viewer.loader import load_detail
from coroner.viewer.model import dump_json, to_detail
from coroner.viewer.timefmt import epoch_ms_to_text
from tests.unit.viewer.builder import InspectSpec, RunsBuilder, sample_specs

RUN = "b-detail-e2e-00000001"
MANIFEST_MS = "2026-11-20T21:13:21.760Z"


def _load(tmp_path, **kwargs):
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run("B", run_id=RUN, **kwargs)
    return load_detail(open_runs_root(tmp_path / "runs"), run_id, DEFAULT_IO, time.time_ns)


def _finished_at_field(record):
    return next(f for f in record.docker_view.fields if f.field == "finished_at")


def test_detail_with_samples_serialises_and_every_tick_label_matches_its_own_x_ms(tmp_path) -> None:
    record = _load(tmp_path, samples=sample_specs(12))

    detail = to_detail(record)
    payload = json.loads(dump_json(detail))

    x_axis = payload["chart"]["x_axis"]
    assert x_axis is not None
    assert x_axis["ticks"]
    for tick in x_axis["ticks"]:
        assert set(tick) == {"x_ms", "label"}
        assert isinstance(tick["x_ms"], int)
        assert tick["label"] == epoch_ms_to_text(tick["x_ms"])


def test_raw_finished_at_with_nine_digits_equal_to_manifest_ms_is_a_match(tmp_path) -> None:
    record = _load(
        tmp_path,
        samples=sample_specs(3),
        inspect=InspectSpec(finished_at="2026-11-20T21:13:21.760545826Z"),
    )

    result = _finished_at_field(record)
    assert result.result == "equal"
    assert result.manifest_value == MANIFEST_MS
    assert result.raw_value == "2026-11-20T21:13:21.760545826Z"
    assert not any(f.field == "finished_at" for f in record.docker_view.discrepancies)


def test_different_raw_finished_at_is_a_discrepancy_reporting_both_values(tmp_path) -> None:
    record = _load(
        tmp_path,
        samples=sample_specs(3),
        inspect=InspectSpec(finished_at="2026-11-20T21:13:22.001999999Z"),
        manifest_extra={"container_finished_at": MANIFEST_MS},
    )

    result = _finished_at_field(record)
    assert result.result == "discrepancy"
    assert result.manifest_value == MANIFEST_MS
    assert result.raw_value == "2026-11-20T21:13:22.001999999Z"
    assert any(f.field == "finished_at" for f in record.docker_view.discrepancies)
    assert record.docker_view.source == "inspect_and_manifest_disagree"


def test_zero_date_raw_finished_at_is_not_recorded(tmp_path) -> None:
    record = _load(
        tmp_path,
        samples=sample_specs(3),
        inspect=InspectSpec(finished_at="0001-01-01T00:00:00Z"),
        manifest_extra={"container_finished_at": MANIFEST_MS},
    )

    result = _finished_at_field(record)
    assert result.result == "not_compared_raw_missing"
    assert result.raw_value is None
    assert record.docker_view.raw is not None
    assert record.docker_view.raw.finished_at is None


# ------------------------------------------------------------------ raw_unavailable_reason through the loader


@pytest.mark.parametrize(
    ("raw_bytes", "reason", "artifact_reason"),
    [
        (b"{this is not json", "invalid_json", "inspect_invalid_json"),
        (b"\xff\xfe not utf-8", "invalid_json", "inspect_invalid_json"),
        (b"{}", "rejected", "inspect_rejected"),
        (b'[{"Id": "short"}]', "rejected", "inspect_rejected"),
        pytest.param(b"[" * 100_000, "rejected", "inspect_rejected", id="deep_nesting"),
    ],
)
def test_loader_derives_invalid_json_and_rejected_from_the_inspect_error_cause(
    tmp_path, raw_bytes: bytes, reason: str, artifact_reason: str,
) -> None:
    record = _load(tmp_path, samples=sample_specs(2), inspect=raw_bytes)

    payload = json.loads(dump_json(to_detail(record)))
    docker = payload["docker_state"]
    assert docker["source"] == "manifest_only" and docker["raw"] is None
    assert docker["raw_unavailable_reason"] == reason
    assert payload["artifact_status"]["inspect"]["status"] == "invalid"
    assert payload["artifact_status"]["inspect"]["reason"] == artifact_reason


def test_loader_keeps_missing_as_the_raw_unavailable_reason_when_inspect_is_absent(tmp_path) -> None:
    record = _load(tmp_path, samples=sample_specs(2), inspect=None)

    docker = json.loads(dump_json(to_detail(record)))["docker_state"]
    assert docker["raw_unavailable_reason"] == "missing"
