"""Evidence assembly tests (task 2.13).

Synthetic fixtures in tmp_path only (RunsBuilder); nothing reads the real runs/ directory.

`load_detail` assembles the RunRecord. The response values are read from the `evidence_summary` block
(`model._ser_evidence_summary_block`). That block is used directly instead of `to_detail` because
`to_detail` currently fails for any run that has Samples (`_ser_chart_block` reads `XAxis.ticks`, which
does not exist). That defect is reported separately and is not pinned here.
"""
from __future__ import annotations

import json
import time

import pytest

from coroner.evidence.reader import UNOBSERVED_NOTE, read_evidence, summarize
from coroner.viewer.files import DEFAULT_IO, open_runs_root
from coroner.viewer.loader import load_detail
from coroner.viewer.model import _ser_evidence_summary_block
from tests.unit.viewer.builder import GapSpec, RunsBuilder, SampleSpec, StateSpec, sample_specs

RUN = "b-evidence-00000001"
BASE = "2026-11-20T21:13:00."


def _load(tmp_path, *, drop: tuple[str, ...] = (), **kwargs):
    """Build one run with RunsBuilder, optionally delete artifacts, load it. Returns (record, block)."""
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run("B", run_id=RUN, **kwargs)
    for name in drop:
        (tmp_path / "runs" / run_id / name).unlink()
    record = load_detail(open_runs_root(tmp_path / "runs"), run_id, DEFAULT_IO, time.time_ns)
    return record, _ser_evidence_summary_block(record)


def _donor_evidence(tmp_path, samples, gaps=()) -> bytes:
    """Valid evidence bytes for RUN (so the file-context run_id rule accepts the records)."""
    donor = RunsBuilder(tmp_path / "donor")
    donor.add_run("B", run_id=RUN, samples=samples, gaps=gaps)
    return (tmp_path / "donor" / RUN / "evidence.jsonl").read_bytes()


def _donor_manifest(tmp_path, mutate) -> bytes:
    donor = RunsBuilder(tmp_path / "donor_manifest")
    donor.add_run("B", run_id=RUN, samples=sample_specs(3))
    data = json.loads((tmp_path / "donor_manifest" / RUN / "manifest.json").read_bytes())
    mutate(data)
    return json.dumps(data).encode("utf-8")


ALL_EVIDENCE_KEYS_WITH_VALUES = {
    "sample_count", "valid_record_count", "manifest_record_count", "record_count_mismatch",
    "missing_sample_count", "unreconciled_drop_count", "unreconciled_drop_uncertain",
    "unreconciled_drops", "unreconciled_drops_total", "unreconciled_drops_truncated",
    "silence_intervals", "silence_intervals_total", "silence_intervals_truncated",
    "malformed_line_count", "malformed_lines", "malformed_lines_total", "malformed_lines_truncated",
    "has_valid_record", "first_rss", "highest_rss", "last_rss", "last_persisted_sample",
    "latest_capture_sample", "last_receive_time_summary", "last_receive_time_summary_display",
    "termination_cause", "unobserved_interval_note", "durability_levels",
}


# ------------------------------------------------------------------ missing evidence


def test_missing_evidence_gives_null_for_every_evidence_value_and_keeps_the_manifest(tmp_path) -> None:
    record, block = _load(tmp_path, samples=sample_specs(3), drop=("evidence.jsonl",))

    assert set(block) == ALL_EVIDENCE_KEYS_WITH_VALUES
    assert all(value is None for value in block.values())
    assert record.evidence_fault.status == "missing"
    assert record.sample_rows == []
    assert record.chart_data is None
    # Manifest-derived values are still there.
    assert record.manifest is not None
    assert record.manifest.run_id == RUN
    assert record.verdict == "PASS"
    assert record.docker_view.effective.exit_code == 137
    assert any(w["code"] == "artifact_evidence_missing" for w in record.viewer_warnings)


# ------------------------------------------------------------------ malformed lines


def test_malformed_lines_are_excluded_from_every_count_and_from_the_chart(tmp_path) -> None:
    good = _donor_evidence(tmp_path, sample_specs(3))
    evidence = good + b"this is not json\n" + b'{"record_type": "sample", \n'

    record, block = _load(tmp_path, evidence_bytes=evidence)

    assert block["sample_count"] == 3
    assert block["malformed_line_count"] == 2
    assert block["malformed_lines_total"] == 2
    assert len(block["malformed_lines"]) == 2
    assert all(set(entry) == {"line_no", "byte_length", "kind"} for entry in block["malformed_lines"])
    # run_metadata plus three samples are the valid records; the two bad lines are not counted.
    assert block["valid_record_count"] == 4
    assert block["has_valid_record"] is True
    assert record.chart_data.total_samples == 3
    assert [p.seq for p in record.chart_data.points] == [0, 1, 2]


def test_only_malformed_lines_gives_sample_count_zero_and_no_valid_record(tmp_path) -> None:
    record, block = _load(tmp_path, evidence_bytes=b"garbage one\n{not json either\n")

    assert record.evidence_fault.status == "ok"
    assert block["sample_count"] == 0
    assert block["malformed_line_count"] == 2
    assert block["valid_record_count"] == 0
    assert block["has_valid_record"] is False
    assert block["first_rss"] is None
    assert block["highest_rss"] is None
    assert block["last_rss"] is None
    assert block["last_persisted_sample"] is None
    assert block["latest_capture_sample"] is None
    assert record.chart_data is None


# ------------------------------------------------------------------ record count mismatch


def test_record_count_mismatch_adds_one_warning(tmp_path) -> None:
    manifest = _donor_manifest(tmp_path, lambda d: d["evidence"].__setitem__("record_count", 999))

    record, block = _load(tmp_path, samples=sample_specs(3), manifest_bytes=manifest)

    assert block["manifest_record_count"] == 999
    assert block["valid_record_count"] == 4
    assert block["record_count_mismatch"] is True
    assert [w["code"] for w in record.viewer_warnings].count("record_count_mismatch") == 1


def test_matching_record_count_claims_no_mismatch(tmp_path) -> None:
    record, block = _load(tmp_path, samples=sample_specs(3))

    assert block["manifest_record_count"] == block["valid_record_count"] == 4
    assert block["record_count_mismatch"] is False
    assert all(w["code"] != "record_count_mismatch" for w in record.viewer_warnings)


def test_no_mismatch_is_claimed_when_the_manifest_has_no_evidence_block(tmp_path) -> None:
    manifest = _donor_manifest(tmp_path, lambda d: d.__setitem__("evidence", None))

    record, block = _load(tmp_path, samples=sample_specs(3), manifest_bytes=manifest)

    assert record.manifest.evidence is None
    assert block["manifest_record_count"] is None
    assert block["record_count_mismatch"] is None
    assert all(w["code"] != "record_count_mismatch" for w in record.viewer_warnings)
    # The evidence file is still read.
    assert block["sample_count"] == 3


# ------------------------------------------------------------------ counts equal summarize


def test_response_counts_equal_summarize_counts(tmp_path) -> None:
    samples = [SampleSpec(seq=s, rss_bytes=50_000_000 + s) for s in (0, 1, 4, 5)]
    gaps = [GapSpec(first_missing_seq=2, last_missing_seq=3)]
    evidence = _donor_evidence(tmp_path, samples, gaps)
    expected = summarize(read_evidence(evidence, name=f"{RUN}.jsonl"))

    _, block = _load(tmp_path, evidence_bytes=evidence)

    assert expected.sample_count == 4
    assert block["sample_count"] == expected.sample_count
    assert block["missing_sample_count"] == expected.missing_sample_count
    assert block["unreconciled_drop_count"] == expected.unreconciled_drop_count
    assert block["malformed_line_count"] == expected.malformed_line_count
    assert block["termination_cause"] == expected.termination_cause
    assert block["last_receive_time_summary"] == expected.last_sample_time


# ------------------------------------------------------------------ last persisted vs latest capture


def test_last_persisted_sample_is_not_the_latest_capture_sample(tmp_path) -> None:
    # Capture_Time order in the file is 10 ms, 30 ms, 20 ms: the last record is not the latest capture.
    samples = [
        SampleSpec(seq=0, ts=BASE + "010Z", rss_bytes=10_000_000),
        SampleSpec(seq=1, ts=BASE + "030Z", rss_bytes=30_000_000),
        SampleSpec(seq=2, ts=BASE + "020Z", rss_bytes=20_000_000),
    ]

    record, block = _load(tmp_path, samples=samples)

    # file_pos counts every record; run_metadata is record 0, so the samples are at 1, 2 and 3.
    assert record.last_persisted_row.ts == BASE + "020Z"
    assert record.last_persisted_row.file_pos == 3
    assert record.latest_capture_row.ts == BASE + "030Z"
    assert record.latest_capture_row.file_pos == 2

    assert block["last_persisted_sample"]["seq"] == 2
    assert block["last_persisted_sample"]["capture_time"] == BASE + "020Z"
    assert block["latest_capture_sample"]["seq"] == 1
    assert block["latest_capture_sample"]["capture_time"] == BASE + "030Z"
    # First RSS is first in Capture order; last RSS is the Last_Persisted_Sample.
    assert block["first_rss"]["seq"] == 0
    assert block["last_rss"]["seq"] == 2
    assert block["last_rss"]["basis"] == "last_persisted"


def test_last_receive_time_summary_is_returned_separately_from_both_samples(tmp_path) -> None:
    samples = [
        SampleSpec(seq=0, ts=BASE + "010Z", recv_time=BASE + "110Z"),
        SampleSpec(seq=1, ts=BASE + "030Z", recv_time=BASE + "130Z"),
        SampleSpec(seq=2, ts=BASE + "020Z", recv_time=BASE + "120Z"),
    ]

    _, block = _load(tmp_path, samples=samples)

    assert block["last_receive_time_summary"] == BASE + "120Z"
    assert block["last_receive_time_summary_display"] == BASE + "120Z"
    assert block["last_persisted_sample"]["receive_time"] == BASE + "120Z"
    # It is a Receive_Time, not the Capture_Time of the latest capture.
    assert block["last_receive_time_summary"] != block["latest_capture_sample"]["capture_time"]


def test_none_received_is_returned_unchanged_with_a_null_display(tmp_path) -> None:
    _, block = _load(tmp_path, samples=[])

    assert block["sample_count"] == 0
    assert block["last_receive_time_summary"] == "none received"
    assert block["last_receive_time_summary_display"] is None


def test_unobserved_interval_note_is_the_m0_constant(tmp_path) -> None:
    _, block = _load(tmp_path, samples=sample_specs(2))

    assert block["unobserved_interval_note"] == UNOBSERVED_NOTE


# ------------------------------------------------------------------ RSS values


def test_rss_above_the_memory_limit_is_returned_unchanged(tmp_path) -> None:
    limit = 134_217_728
    over = 500_000_000
    samples = [SampleSpec(seq=0, rss_bytes=10_000_000), SampleSpec(seq=1, rss_bytes=over)]

    record, block = _load(tmp_path, samples=samples, container_state=StateSpec(memory=limit))

    assert over > limit
    assert block["highest_rss"]["rss_bytes"] == over
    assert block["last_rss"]["rss_bytes"] == over
    assert record.docker_view.effective.memory_limit_bytes == limit
    assert [p.rss_mib for p in record.chart_data.points][-1] == pytest.approx(over / (1024 * 1024))


def test_null_rss_is_unplottable_and_never_plotted(tmp_path) -> None:
    samples = [
        SampleSpec(seq=0, rss_bytes=1_000_000),
        SampleSpec(seq=1, rss_bytes=None),
        SampleSpec(seq=2, rss_bytes=3_000_000),
    ]

    record, block = _load(tmp_path, samples=samples)

    assert block["sample_count"] == 3
    chart = record.chart_data
    assert chart.total_samples == 3
    assert chart.plottable_samples == 2
    assert chart.unplottable_samples == 1
    assert [p.seq for p in chart.points] == [0, 2]
    assert block["first_rss"]["seq"] == 0
    assert block["highest_rss"]["seq"] == 2
    # The null-RSS Sample is the segment break between the two plotted points.
    assert chart.points[1].skipped_before == 1
    assert chart.points[1].segment_before == "dashed"
