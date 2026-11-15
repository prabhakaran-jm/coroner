"""Tests for coroner.evidence.reader.summarize (task 2.12; requirements 6.5 to 6.7, 6.11 to 6.13, 12.5).

Deterministic examples. The Hypothesis properties for the summary are task 2.17 and are not here.
"""
from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

import pytest

from coroner.evidence.gaps import GapRecordError
from coroner.evidence.reader import ReadResult, RunSummary, read_evidence, summarize
from coroner.evidence.repair import repair_file
from coroner.evidence.writer import Durability, EvidenceWriter, serialize_record

RECV = "2025-01-01T12:00:06.530Z"
NOW = "2025-01-01T12:05:00.004Z"


def ts(seq: int) -> str:
    return f"2025-01-01T12:00:{seq % 60:02d}.512Z"


def sample(seq: int = 0, run_id: str = "r1") -> dict:
    body = {
        "run_id": run_id, "seq": seq, "ts": ts(seq), "rss_bytes": 71303168, "request_count": 9, "loop_delay_ms": 0.84, "pid": 1,
        "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo", "interval_ms": 500, "dropped": 0,
    }
    return {"record_type": "sample", "schema_version": 1, "run_id": run_id, "recv_time": RECV, "sample": body}


def envelope(record_type: str, **fields: object) -> dict:
    return {"record_type": record_type, "schema_version": 1, "run_id": "r1", "recv_time": RECV, **fields}


def disc(gap_id: str, lo: int, hi: int, rec: str = "none", with_: list | None = None, at: int | None = None) -> dict:
    return envelope(
        "gap", gap_id=gap_id, gap_type="sequence_discontinuity", first_missing_seq=lo, last_missing_seq=hi,
        missing_count=hi - lo + 1, reconciliation=rec, reconciles_with=with_ or [], detected_at_seq=hi + 1 if at is None else at,
    )


def drop(gap_id: str, count: int, lo: int | None = None, hi: int | None = None, status: str = "known", rec: str = "none", with_: list | None = None, at: int | None = None) -> dict:
    return envelope(
        "gap", gap_id=gap_id, gap_type="probe_reported_drop", dropped_count=count, range_status=status, first_missing_seq=lo,
        last_missing_seq=hi, reconciliation=rec, reconciles_with=with_ or [], detected_at_seq=(hi or 0) + 1 if at is None else at,
    )


def silence(gap_id: str = "gap-s", last: str = "2025-01-01T12:00:10.530Z", detected: str = "2025-01-01T12:00:12.081Z") -> dict:
    return envelope(
        "gap", gap_id=gap_id, gap_type="silence_timeout", last_sample_time=last, detected_time=detected, threshold_ms=1500,
        interval_ms=500, sample_count="unknown", reconciliation="not_applicable", reconciles_with=[],
    )


def meta(level: str = "crash") -> dict:
    return envelope("run_metadata", event="created", durability=level, fsync_before_ack=level == "crash")


def anomaly(kind: str) -> dict:
    return envelope("sequence_anomaly", anomaly=kind, seq=1, previous_seq=2)


def rejection() -> dict:
    return {"record_type": "rejection", "schema_version": 1, "run_id": None, "recv_time": RECV, "http_status": 422, "reason_code": "invalid_json"}


def termination(label: str | None = "docker_oom_kill") -> dict:
    extra = {} if label is None else {"cause_label": label}
    return envelope("termination", source="experiment_runner", observed_time=RECV, **extra)


def lines(*records: dict) -> bytes:
    return b"".join(serialize_record(r) for r in records)


def summary_of(*records: dict, tail: bytes = b"", name: str = "r1.jsonl") -> RunSummary:
    return summarize(read_evidence(lines(*records) + tail, name=name))


# --- empty, none received, plain ---------------------------------------------------------------------------


def test_empty_evidence() -> None:
    s = summary_of()
    assert (s.run_id, s.sample_count, s.missing_sample_count, s.unreconciled_drop_count) == (None, 0, 0, 0)
    assert s.unreconciled_drop_uncertain is False and s.gap_count == 0 and s.gap_counts_by_type == {}
    assert s.last_sample_time == "none received" and s.termination is None and s.termination_cause == "unknown"
    assert s.unobserved_interval_note is None and s.durability_levels == [] and s.read_errors == [] and s.warnings == []


def test_valid_samples_are_counted_and_the_last_sample_time_is_the_last_ts() -> None:
    s = summary_of(meta(), sample(0), sample(1), sample(2))
    assert (s.run_id, s.sample_count, s.last_sample_time, s.malformed_line_count) == ("r1", 3, ts(2), 0)
    assert s.unobserved_interval_note is None and s.warnings == []


def test_none_received_when_the_run_has_only_non_sample_records() -> None:
    s = summary_of(meta(), disc("gap-1", 0, 2), anomaly("duplicate"))
    assert s.sample_count == 0 and s.last_sample_time == "none received"


# --- malformed, repaired, incomplete -----------------------------------------------------------------------


def test_malformed_lines_are_counted_once_and_never_as_samples() -> None:
    good = lines(sample(0)) + b"garbage\n" + b"[1]\n" + b"\xff\n" + lines(sample(1))
    s = summarize(read_evidence(good, name="r1.jsonl"))
    assert s.sample_count == 2 and s.malformed_line_count == 3 and len(s.read_errors) == 3 and s.last_sample_time == ts(1)


def test_an_unterminated_last_line_is_malformed_even_if_it_parses_and_is_not_counted() -> None:
    s = summarize(read_evidence(lines(sample(0)) + serialize_record(sample(1)).rstrip(b"\n"), name="r1.jsonl"))
    assert s.sample_count == 1 and s.malformed_line_count == 1 and s.last_sample_time == ts(0)
    assert s.read_errors[0].kind == "incomplete_last_line"


def test_a_repaired_file_does_not_count_the_isolated_sample_or_the_repair_record(tmp_path: Path) -> None:
    directory = tmp_path / "ev"
    directory.mkdir()
    path = directory / "r1.jsonl"
    path.write_bytes(lines(sample(0)) + serialize_record(sample(1)).rstrip(b"\n"))  # a valid Sample, never acknowledged
    writer = EvidenceWriter(directory, Durability.PROCESS)
    assert repair_file(path, writer, lambda: NOW)["action"] == "appended_newline"
    writer.append("r1", [sample(2)])
    s = summarize(read_evidence(path))
    assert s.sample_count == 2 and s.malformed_line_count == 1 and s.last_sample_time == ts(2)
    assert [e.kind for e in s.read_errors] == ["isolated_by_repair"]
    assert s.rejection_count == 0 and s.gap_count == 0


# --- termination and the unobserved interval --------------------------------------------------------------


def test_without_a_termination_record_the_cause_is_unknown_and_there_is_no_note() -> None:
    s = summary_of(sample(0))
    assert s.termination is None and s.termination_cause == "unknown" and s.unobserved_interval_note is None


def test_a_termination_record_gives_its_label_and_the_unobserved_note() -> None:
    s = summary_of(sample(0), termination("docker_oom_kill"))
    assert s.termination["record_type"] == "termination" and s.termination_cause == "docker_oom_kill"
    assert s.unobserved_interval_note and "unobserved" in s.unobserved_interval_note


def test_a_termination_record_without_a_label_is_not_guessed() -> None:
    s = summary_of(sample(0), envelope("termination", source="experiment_runner", container_state={"oom_killed": True, "exit_code": 137}))
    assert s.termination is not None and s.termination_cause == "unknown" and s.unobserved_interval_note


def test_the_last_termination_record_wins_and_a_warning_says_so() -> None:
    s = summary_of(termination("a"), termination("b"))
    assert s.termination_cause == "b" and any("2 termination records" in w for w in s.warnings)


def test_a_quarantined_termination_record_does_not_count() -> None:
    s = summarize(read_evidence(lines(sample(0)) + serialize_record(termination()).rstrip(b"\n"), name="r1.jsonl"))
    assert s.termination is None and s.termination_cause == "unknown" and s.unobserved_interval_note is None


# --- silence -----------------------------------------------------------------------------------------------


def test_a_silence_gap_is_an_interval_with_an_unknown_sample_count_and_adds_to_no_count() -> None:
    s = summary_of(sample(0), silence())
    assert s.silence_intervals == [
        {"last_sample_time": "2025-01-01T12:00:10.530Z", "detected_time": "2025-01-01T12:00:12.081Z", "sample_count": "unknown"}
    ]
    assert s.gap_count == 1 and s.gap_counts_by_type == {"silence_timeout": 1}
    assert (s.missing_sample_count, s.unreconciled_drop_count, s.unreconciled_drop_uncertain) == (0, 0, False)
    assert s.unobserved_interval_note is not None and s.warnings == []  # open: no Sample follows it


def test_a_silence_gap_followed_by_a_sample_is_not_open() -> None:
    s = summary_of(sample(0), silence(), sample(1))
    assert len(s.silence_intervals) == 1 and s.unobserved_interval_note is None


# --- gap numbers -------------------------------------------------------------------------------------------


def test_discontinuity_with_an_identical_drop_counts_once_and_adds_no_unreconciled_drop() -> None:
    s = summary_of(
        disc("gap-0001", 13, 15, "reconciled", ["gap-0002"]), drop("gap-0002", 3, 13, 15, rec="reconciled", with_=["gap-0001"])
    )
    assert (s.missing_sample_count, s.unreconciled_drop_count, s.unreconciled_drop_uncertain) == (3, 0, False)
    assert s.unreconciled_drops == [] and s.gap_counts_by_type == {"sequence_discontinuity": 1, "probe_reported_drop": 1}
    assert s.warnings == []


def test_a_partly_overlapping_drop_leaves_the_uncovered_numbers_unreconciled_and_uncertain() -> None:
    s = summary_of(
        disc("gap-0001", 13, 15, "partial_overlap", ["gap-0002"], at=18), drop("gap-0002", 4, 14, 17, rec="partial_overlap", with_=["gap-0001"], at=18)
    )
    assert (s.missing_sample_count, s.unreconciled_drop_count, s.unreconciled_drop_uncertain) == (3, 2, True)
    assert s.unreconciled_drops == [{"gap_id": "gap-0002", "count": 2, "uncertain": True, "reason": "range not covered by a sequence discontinuity"}]
    assert s.warnings == []


def test_an_unknown_range_drop_adds_its_dropped_count_and_sets_the_flag() -> None:
    s = summary_of(drop("gap-0003", 4, status="unknown"))
    assert (s.missing_sample_count, s.unreconciled_drop_count, s.unreconciled_drop_uncertain) == (0, 4, True)
    assert s.unreconciled_drops[0]["gap_id"] == "gap-0003" and s.unreconciled_drops[0]["uncertain"] is True


def test_an_uncertain_range_drop_counts_dropped_count_and_a_known_drop_outside_every_discontinuity_counts_in_full() -> None:
    s = summary_of(drop("gap-1", 2, status="uncertain"), drop("gap-2", 3, 20, 22))
    assert s.unreconciled_drop_count == 5 and s.unreconciled_drop_uncertain is True and s.missing_sample_count == 0


def test_overlapping_and_duplicated_discontinuities_count_each_number_once() -> None:
    s = summary_of(disc("gap-1", 5, 9), disc("gap-2", 8, 12), disc("gap-3", 5, 9))
    assert s.missing_sample_count == 8  # 5..12


def test_no_drop_means_no_flag() -> None:
    s = summary_of(disc("gap-1", 0, 3))
    assert s.unreconciled_drop_uncertain is False and s.unreconciled_drop_count == 0 and s.missing_sample_count == 4


def test_a_malformed_gap_payload_raises_instead_of_being_skipped() -> None:
    with pytest.raises(GapRecordError):
        summary_of(envelope("gap", gap_id="gap-1", gap_type="sequence_discontinuity", first_missing_seq=5, last_missing_seq=2))
    with pytest.raises(GapRecordError):
        summary_of(envelope("gap", gap_id="gap-1", gap_type="probe_reported_drop", range_status="known", dropped_count=1))


def test_an_unrecognised_gap_type_is_counted_by_name_and_warned_about() -> None:
    s = summary_of(envelope("gap", gap_id="gap-1", gap_type="mystery"), envelope("gap", gap_id="gap-2", gap_type=7))
    assert s.gap_count == 2 and s.gap_counts_by_type == {"mystery": 1, "<invalid>": 1}
    assert sum("unrecognised gap_type" in w for w in s.warnings) == 2


# --- reconciliation warning --------------------------------------------------------------------------------


def test_a_stored_reconciliation_that_disagrees_with_the_ranges_warns_and_changes_no_count() -> None:
    s = summary_of(disc("gap-0001", 13, 15, "none"), drop("gap-0002", 3, 13, 15, rec="reconciled", with_=["gap-0001"]))
    assert len(s.warnings) == 1 and "disagrees with the ranges" in s.warnings[0]  # only the discontinuity is marked wrongly
    assert "gap-0001" in s.warnings[0] and "'reconciled'" in s.warnings[0]
    assert (s.missing_sample_count, s.unreconciled_drop_count) == (3, 0)


def test_a_wrong_reconciles_with_list_warns_and_a_partial_overlap_is_recomputed() -> None:
    s = summary_of(disc("gap-1", 13, 15, "reconciled", ["gap-2"], at=18), drop("gap-2", 4, 14, 17, "known", "reconciled", ["gap-1"], at=18))
    assert len(s.warnings) == 2  # identical-range marks are wrong: the ranges differ, so both are partial_overlap
    assert "'partial_overlap'" in s.warnings[0]


def test_overlapping_gaps_from_different_ingests_do_not_warn_and_counts_are_unchanged() -> None:
    s = summary_of(disc("gap-1", 5, 9, at=10), disc("gap-2", 8, 12, at=13))
    assert s.warnings == [] and s.missing_sample_count == 8 and s.unreconciled_drop_count == 0


def test_a_wrong_mark_within_one_ingest_warns_and_a_correct_one_does_not() -> None:
    bad = summary_of(disc("gap-1", 13, 15, "none", at=16), drop("gap-2", 3, 13, 15, rec="none", at=16))
    assert len(bad.warnings) == 2 and (bad.missing_sample_count, bad.unreconciled_drop_count) == (3, 0)
    good = summary_of(disc("gap-1", 13, 15, "reconciled", ["gap-2"], at=16), drop("gap-2", 3, 13, 15, rec="reconciled", with_=["gap-1"], at=16))
    assert good.warnings == [] and (good.missing_sample_count, good.unreconciled_drop_count) == (3, 0)


def test_a_gap_marked_against_another_ingest_warns_because_the_collector_never_does_that() -> None:
    s = summary_of(disc("gap-1", 5, 9, "partial_overlap", ["gap-2"], at=10), disc("gap-2", 8, 12, "partial_overlap", ["gap-1"], at=13))
    assert len(s.warnings) == 2 and s.missing_sample_count == 8


def test_an_exact_duplicate_gap_record_is_the_same_gap_and_causes_no_warning() -> None:
    s = summary_of(disc("gap-1", 5, 9, at=10), disc("gap-1", 5, 9, at=10))
    assert s.warnings == [] and s.missing_sample_count == 5


def test_a_missing_reconciliation_field_warns_and_a_silence_gap_is_not_applicable() -> None:
    bad = disc("gap-1", 0, 1)
    del bad["reconciliation"]
    s = summary_of(bad, silence())
    assert len(s.warnings) == 1 and "gap-1" in s.warnings[0]


# --- duplicates, resets, rejections, durability ------------------------------------------------------------


def test_duplicates_resets_and_rejections_are_counted_by_type() -> None:
    s = summary_of(anomaly("duplicate"), anomaly("duplicate"), anomaly("reset"), sample(0))
    assert (s.duplicate_count, s.reset_count, s.sample_count) == (2, 1, 1)
    r = summarize(read_evidence(lines(rejection(), rejection()), name="_rejections.jsonl"))
    assert (r.rejection_count, r.run_id, r.sample_count) == (2, None, 0)


def test_durability_levels_are_distinct_and_in_first_seen_order() -> None:
    assert summary_of(meta("process"), sample(0), meta("crash"), meta("process")).durability_levels == ["process", "crash"]


# --- determinism, purity, contract ------------------------------------------------------------------------------


def test_summarize_is_deterministic_and_does_not_change_its_input() -> None:
    result = read_evidence(
        lines(meta(), sample(0), disc("gap-1", 3, 4), drop("gap-2", 2, status="unknown"), silence(), termination()) + b"junk\n", name="r1.jsonl"
    )
    before = copy.deepcopy(result)
    first, second = summarize(result), summarize(result)
    assert first == second and result == before
    first.unreconciled_drops.append({"x": 1})
    first.termination["cause_label"] = "changed"
    assert summarize(result) == second  # mutating the returned summary does not reach the evidence


def test_run_summary_has_exactly_the_designed_fields() -> None:
    assert [f.name for f in dataclasses.fields(RunSummary)] == [
        "run_id", "sample_count", "missing_sample_count", "unreconciled_drop_count", "unreconciled_drop_uncertain", "gap_count",
        "gap_counts_by_type", "silence_intervals", "unreconciled_drops", "malformed_line_count", "duplicate_count", "reset_count",
        "rejection_count", "last_sample_time", "termination", "termination_cause", "unobserved_interval_note", "durability_levels",
        "read_errors", "warnings",
    ]


def test_summarize_takes_a_read_result_built_by_hand() -> None:
    s = summarize(ReadResult(path="x", records=[sample(0)], line_numbers=[1], errors=[]))
    assert s.sample_count == 1 and s.run_id == "r1"
