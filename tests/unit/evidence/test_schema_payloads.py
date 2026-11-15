"""Tests for the payload contracts of the six non-sample record types (task 2.19; requirements 5.5, 6.8, 12.2).

The records are the design's "Data Models" examples (the rejection preview and the repair `fragment_len` were corrected
in the design in the D31 pass, and `test_the_design_examples_are_valid...` reads them from design.md itself). Task 2.19
corrections (D31) are covered at the end of this file. Everything calls the real code.
"""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coroner.evidence import repair, schema, writer
from coroner.evidence.gaps import GapRecordError
from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.schema import validate_record
from coroner.evidence.writer import Durability, EvidenceWriter, RecordError, serialize_record

RUN = "a-20250101T120000Z-9f3a1c2e"
RECV = "2025-01-01T12:00:08.031Z"
DEL = object()
SAMPLE = {
    "run_id": "r1", "seq": 7, "ts": "2025-01-01T12:00:03.500Z", "rss_bytes": 50000000, "request_count": 1, "loop_delay_ms": 0.2, "pid": 1,
    "container_id": None, "container_id_source": "none", "interval_ms": 500, "dropped": 0, "null_reasons": {"container_id": "not found"},
}
GAP = {"record_type": "gap", "schema_version": 1, "run_id": RUN, "recv_time": RECV, "start_time": "2025-01-01T12:00:06.530Z", "end_time": RECV}


def _ex(base: dict, **fields: object) -> dict:
    return {**base, **fields}


ENV = {"schema_version": 1, "run_id": RUN, "recv_time": RECV}
EXAMPLES: dict[str, dict] = {
    "meta": {"record_type": "run_metadata", **ENV, "event": "created", "durability": "crash", "fsync_before_ack": True,
             "evidence_file": RUN + ".jsonl", "collector_version": "0.1.0"},
    "disc": _ex(GAP, gap_id="gap-0001", gap_type="sequence_discontinuity", first_missing_seq=13, last_missing_seq=15, missing_count=3,
                reconciliation="reconciled", reconciles_with=["gap-0002"], detected_at_seq=16),
    "known": _ex(GAP, gap_id="gap-0002", gap_type="probe_reported_drop", dropped_count=3, range_status="known", first_missing_seq=13,
                 last_missing_seq=15, reconciliation="reconciled", reconciles_with=["gap-0001"], detected_at_seq=16),
    "unknown": _ex(GAP, run_id="r1", gap_id="gap-0003", gap_type="probe_reported_drop", dropped_count=1, range_status="unknown",
                   first_missing_seq=None, last_missing_seq=None, range_note="range overlaps received samples", reconciliation="none",
                   reconciles_with=[], detected_at_seq=20),
    "silence": _ex(GAP, gap_id="gap-0004", gap_type="silence_timeout", last_sample_time="2025-01-01T12:00:10.530Z",
                   detected_time="2025-01-01T12:00:12.081Z", threshold_ms=1500, interval_ms=500, sample_count="unknown",
                   reconciliation="not_applicable", reconciles_with=[]),
    "anomaly": {"record_type": "sequence_anomaly", **ENV, "run_id": "r1", "anomaly": "duplicate", "seq": 7, "previous_seq": 9, "sample": SAMPLE},
    "reject": {"record_type": "rejection", **ENV, "run_id": None, "http_status": 422, "reason_code": "invalid_run_id", "field": "run_id",
               "reason": "run_id longer than 64 characters", "body_len": 214, "body_preview_b64": "eyJydW5faWQiOiI="},
    "repair": {"record_type": "repair", **ENV, "file": RUN + ".jsonl", "line_no": 143, "fragment_len": 42,
               "fragment_b64": "eyJyZWNvcmRfdHlwZSI6InNhbXBsZSIsInNjaGVtYV92ZXJzaW9uIjox", "fragment_truncated": False,
               "reason": "incomplete_last_line", "action": "appended_newline"},
    "term": {"record_type": "termination", **ENV, "run_id": "c-20250101T121500Z-4be07a11", "source": "experiment_runner",
             "observed_time": "2025-01-01T12:17:03.221Z", "container_state": {"status": "exited", "exit_code": 137, "oom_killed": True},
             "cause_label": "docker_oom_kill", "note": "Observed after the fact via docker inspect."},
}


def make(name: str, *edits: tuple[str, object]) -> dict:
    """A deep copy of an example with dotted-path edits; the value DEL deletes the key."""
    record = copy.deepcopy(EXAMPLES[name])
    for path, value in edits:
        *parents, last = path.split(".")
        target = record
        for key in parents:
            target = target[key]
        if value is DEL:
            del target[last]
        else:
            target[last] = value
    return record


def found(record: object) -> set[tuple[str, str]]:
    return {(i.field, i.reason_code) for i in validate_record(record)}


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def test_every_design_example_is_valid_and_every_non_sample_type_has_one() -> None:
    for name in EXAMPLES:
        assert validate_record(make(name)) == [], name
    assert {r["record_type"] for r in EXAMPLES.values()} == set(schema.RECORD_TYPES) - {"sample"}


def test_the_constants_agree_with_the_writer_and_repair_modules() -> None:
    assert (schema.FILE_SUFFIX, schema.REJECTIONS_STEM) == (writer.FILE_SUFFIX, writer.REJECTIONS_STEM)
    assert schema.FRAGMENT_CAP == repair.FRAGMENT_CAP


# --- missing required fields: one parametrization per type --------------------------------------------------------

REQUIRED = {
    "meta": ["event", "durability", "fsync_before_ack", "evidence_file", "collector_version"],
    "disc": ["gap_id", "gap_type", "start_time", "end_time", "first_missing_seq", "last_missing_seq", "missing_count", "reconciliation",
             "reconciles_with", "detected_at_seq"],
    "known": ["gap_id", "gap_type", "start_time", "end_time", "dropped_count", "range_status", "first_missing_seq", "last_missing_seq",
              "reconciliation", "reconciles_with", "detected_at_seq"],
    "silence": ["gap_id", "gap_type", "start_time", "end_time", "last_sample_time", "detected_time", "threshold_ms", "interval_ms", "sample_count",
                "reconciliation", "reconciles_with"],
    "anomaly": ["anomaly", "seq", "previous_seq", "sample"],
    "reject": ["http_status", "reason_code", "field", "reason", "body_len", "body_preview_b64"],
    "repair": ["file", "line_no", "fragment_len", "fragment_b64", "fragment_truncated", "reason", "action"],
    "term": ["source", "observed_time", "container_state", "cause_label", "note"],
}
MISSING = [(name, field) for name, fields in REQUIRED.items() for field in fields]


@pytest.mark.parametrize("name,field", MISSING, ids=[f"{n}-{f}" for n, f in MISSING])
def test_each_required_field_is_reported_missing(name: str, field: str) -> None:
    assert found(make(name, (field, DEL))) == {(field, "missing_field")}


@pytest.mark.parametrize("name,field", [("term", "container_state.status"), ("term", "container_state.exit_code"), ("term", "container_state.oom_killed")])
def test_each_container_state_field_is_required(name: str, field: str) -> None:
    assert found(make(name, (field, DEL))) == {(field, "missing_field")}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_an_extra_top_level_field_is_unknown(name: str) -> None:
    assert found(make(name, ("zzz_extra", 1))) == {("zzz_extra", "unknown_field")}


def test_unknown_fields_are_reported_in_sorted_order_whatever_the_insertion_order() -> None:
    forward = make("meta", ("b_extra", 1), ("a_extra", 2))
    backward = dict(reversed(list(forward.items())))
    first = [i.field for i in validate_record(forward)]
    assert first == ["a_extra", "b_extra"] == [i.field for i in validate_record(backward)]


def test_the_container_state_rejects_unknown_fields() -> None:
    assert found(make("term", ("container_state.pid", 1))) == {("container_state.pid", "unknown_field")}


# --- wrong values: (name, edits, expected issue) ------------------------------------------------------------------

CASES: list[tuple[str, str, list[tuple[str, object]], tuple[str, str]]] = [
    # run_metadata
    ("meta-event-value", "meta", [("event", "restarted")], ("event", "out_of_range")),
    ("meta-event-type", "meta", [("event", 1)], ("event", "wrong_type")),
    ("meta-dur-value", "meta", [("durability", "fast")], ("durability", "out_of_range")),
    ("meta-dur-null", "meta", [("durability", None)], ("durability", "wrong_type")),
    ("meta-fsync-str", "meta", [("fsync_before_ack", "true")], ("fsync_before_ack", "wrong_type")),
    ("meta-fsync-int", "meta", [("fsync_before_ack", 1)], ("fsync_before_ack", "wrong_type")),
    ("meta-crash-nofsync", "meta", [("fsync_before_ack", False)], ("fsync_before_ack", "out_of_range")),
    ("meta-process-fsync", "meta", [("durability", "process")], ("fsync_before_ack", "out_of_range")),
    ("meta-file-suffix", "meta", [("evidence_file", "r1.txt")], ("evidence_file", "invalid_format")),
    ("meta-file-rejections", "meta", [("evidence_file", "_rejections.jsonl")], ("evidence_file", "invalid_format")),
    ("meta-file-badstem", "meta", [("evidence_file", "-x.jsonl")], ("evidence_file", "invalid_format")),
    ("meta-file-path", "meta", [("evidence_file", "../r1.jsonl")], ("evidence_file", "invalid_format")),
    ("meta-file-type", "meta", [("evidence_file", 5)], ("evidence_file", "wrong_type")),
    ("meta-version-type", "meta", [("collector_version", 1)], ("collector_version", "wrong_type")),
    # sequence_discontinuity
    ("disc-id-empty", "disc", [("gap_id", "")], ("gap_id", "out_of_range")),
    ("disc-id-type", "disc", [("gap_id", 1)], ("gap_id", "wrong_type")),
    ("disc-type-value", "disc", [("gap_type", "mystery")], ("gap_type", "out_of_range")),
    ("disc-type-type", "disc", [("gap_type", 7)], ("gap_type", "wrong_type")),
    ("disc-start-bad", "disc", [("start_time", "yesterday")], ("start_time", "invalid_format")),
    ("disc-end-type", "disc", [("end_time", 5)], ("end_time", "wrong_type")),
    ("disc-first-bool", "disc", [("first_missing_seq", True), ("missing_count", 15), ("last_missing_seq", 15)], ("first_missing_seq", "wrong_type")),
    ("disc-first-neg", "disc", [("first_missing_seq", -1)], ("first_missing_seq", "out_of_range")),
    ("disc-last-str", "disc", [("last_missing_seq", "15")], ("last_missing_seq", "wrong_type")),
    ("disc-first-gt-last", "disc", [("first_missing_seq", 16)], ("last_missing_seq", "out_of_range")),
    ("disc-count-off", "disc", [("missing_count", 4)], ("missing_count", "out_of_range")),
    ("disc-count-zero", "disc", [("missing_count", 0)], ("missing_count", "out_of_range")),
    ("disc-count-float", "disc", [("missing_count", 3.0)], ("missing_count", "wrong_type")),
    ("disc-at-neg", "disc", [("detected_at_seq", -1)], ("detected_at_seq", "out_of_range")),
    ("disc-at-null", "disc", [("detected_at_seq", None)], ("detected_at_seq", "wrong_type")),
    ("disc-rec-value", "disc", [("reconciliation", "maybe")], ("reconciliation", "out_of_range")),
    ("disc-with-type", "disc", [("reconciles_with", "gap-2")], ("reconciles_with", "wrong_type")),
    ("disc-with-item", "disc", [("reconciles_with", [1])], ("reconciles_with[0]", "wrong_type")),
    ("disc-with-empty", "disc", [("reconciles_with", ["gap-1", ""])], ("reconciles_with[1]", "out_of_range")),
    ("disc-silence-field", "disc", [("threshold_ms", 1500)], ("threshold_ms", "unknown_field")),
    # probe_reported_drop
    ("drop-count-zero", "known", [("dropped_count", 0)], ("dropped_count", "out_of_range")),
    ("drop-count-big", "known", [("dropped_count", 4_294_967_296)], ("dropped_count", "out_of_range")),
    ("drop-count-bool", "known", [("dropped_count", True)], ("dropped_count", "wrong_type")),
    ("drop-status-value", "known", [("range_status", "guess")], ("range_status", "out_of_range")),
    ("drop-status-type", "known", [("range_status", None)], ("range_status", "wrong_type")),
    ("drop-known-null-first", "known", [("first_missing_seq", None)], ("first_missing_seq", "wrong_type")),
    ("drop-known-null-last", "known", [("last_missing_seq", None)], ("last_missing_seq", "wrong_type")),
    ("drop-known-first-gt", "known", [("first_missing_seq", 16)], ("last_missing_seq", "out_of_range")),
    ("drop-known-neg", "known", [("first_missing_seq", -1)], ("first_missing_seq", "out_of_range")),
    ("drop-known-bool", "known", [("last_missing_seq", True)], ("last_missing_seq", "wrong_type")),
    ("drop-unknown-first", "unknown", [("first_missing_seq", 5)], ("first_missing_seq", "out_of_range")),
    ("drop-unknown-last", "unknown", [("last_missing_seq", 5)], ("last_missing_seq", "out_of_range")),
    ("drop-uncertain-first", "unknown", [("range_status", "uncertain"), ("first_missing_seq", 5)], ("first_missing_seq", "out_of_range")),
    ("drop-note-type", "unknown", [("range_note", 5)], ("range_note", "wrong_type")),
    ("drop-no-missing-count", "known", [("missing_count", 3)], ("missing_count", "unknown_field")),
    # silence_timeout
    ("sil-count-num", "silence", [("sample_count", 3)], ("sample_count", "wrong_type")),
    ("sil-count-str", "silence", [("sample_count", "3")], ("sample_count", "out_of_range")),
    ("sil-last-bad", "silence", [("last_sample_time", "2025-01-01")], ("last_sample_time", "invalid_format")),
    ("sil-detected-type", "silence", [("detected_time", 1)], ("detected_time", "wrong_type")),
    ("sil-threshold-str", "silence", [("threshold_ms", "1500")], ("threshold_ms", "wrong_type")),
    ("sil-threshold-neg", "silence", [("threshold_ms", -1)], ("threshold_ms", "out_of_range")),
    ("sil-interval-low", "silence", [("interval_ms", 99)], ("interval_ms", "out_of_range")),
    ("sil-interval-high", "silence", [("interval_ms", 60001)], ("interval_ms", "out_of_range")),
    ("sil-seq-field", "silence", [("first_missing_seq", 1)], ("first_missing_seq", "unknown_field")),
    # sequence_anomaly
    ("anom-kind", "anomaly", [("anomaly", "replay")], ("anomaly", "out_of_range")),
    ("anom-kind-type", "anomaly", [("anomaly", None)], ("anomaly", "wrong_type")),
    ("anom-seq-bool", "anomaly", [("seq", True)], ("seq", "wrong_type")),
    ("anom-seq-neg", "anomaly", [("seq", -1)], ("seq", "out_of_range")),
    ("anom-prev-str", "anomaly", [("previous_seq", "9")], ("previous_seq", "wrong_type")),
    ("anom-prev-neg", "anomaly", [("previous_seq", -2)], ("previous_seq", "out_of_range")),
    ("anom-sample-type", "anomaly", [("sample", [1])], ("sample", "wrong_type")),
    ("anom-sample-bad", "anomaly", [("sample.rss_bytes", -1)], ("sample.rss_bytes", "out_of_range")),
    ("anom-sample-missing", "anomaly", [("sample.pid", DEL)], ("sample.pid", "missing_field")),
    ("anom-sample-extra", "anomaly", [("sample.zzz", 1)], ("sample.zzz", "unknown_field")),
    ("anom-sample-runid", "anomaly", [("sample.run_id", "other")], ("run_id", "invalid_run_id")),
    ("anom-sample-seq", "anomaly", [("sample.seq", 8)], ("seq", "out_of_range")),
    # rejection
    ("rej-status-low", "reject", [("http_status", 99)], ("http_status", "out_of_range")),
    ("rej-status-high", "reject", [("http_status", 600)], ("http_status", "out_of_range")),
    ("rej-status-str", "reject", [("http_status", "422")], ("http_status", "wrong_type")),
    ("rej-status-bool", "reject", [("http_status", True)], ("http_status", "wrong_type")),
    ("rej-code", "reject", [("reason_code", "nope")], ("reason_code", "out_of_range")),
    ("rej-code-type", "reject", [("reason_code", 1)], ("reason_code", "wrong_type")),
    ("rej-field-type", "reject", [("field", None)], ("field", "wrong_type")),
    ("rej-reason-type", "reject", [("reason", 5)], ("reason", "wrong_type")),
    ("rej-len-neg", "reject", [("body_len", -1)], ("body_len", "out_of_range")),
    ("rej-len-bool", "reject", [("body_len", False)], ("body_len", "wrong_type")),
    ("rej-len-float", "reject", [("body_len", 2.0)], ("body_len", "wrong_type")),
    ("rej-b64-dots", "reject", [("body_preview_b64", "eyJydW5faWQiOi...")], ("body_preview_b64", "invalid_format")),
    ("rej-b64-space", "reject", [("body_preview_b64", "eHh4 ")], ("body_preview_b64", "invalid_format")),
    ("rej-b64-newline", "reject", [("body_preview_b64", "eHh4\n")], ("body_preview_b64", "invalid_format")),
    ("rej-b64-inner-ws", "reject", [("body_preview_b64", "eH h4")], ("body_preview_b64", "invalid_format")),
    ("rej-b64-padding", "reject", [("body_preview_b64", "eHh")], ("body_preview_b64", "invalid_format")),
    ("rej-b64-noncanon", "reject", [("body_preview_b64", "eB==")], ("body_preview_b64", "invalid_format")),
    ("rej-b64-nonascii", "reject", [("body_preview_b64", "eHh4\u00e9")], ("body_preview_b64", "invalid_format")),
    ("rej-b64-urlsafe", "reject", [("body_preview_b64", "-_-_")], ("body_preview_b64", "invalid_format")),
    ("rej-b64-type", "reject", [("body_preview_b64", 5)], ("body_preview_b64", "wrong_type")),
    ("rej-b64-1025", "reject", [("body_preview_b64", b64(b"x" * 1025)), ("body_len", 5000)], ("body_preview_b64", "out_of_range")),
    # repair
    ("rep-file-type", "repair", [("file", 5)], ("file", "wrong_type")),
    ("rep-file-bad", "repair", [("file", "notes.txt")], ("file", "invalid_format")),
    ("rep-line-zero", "repair", [("line_no", 0)], ("line_no", "out_of_range")),
    ("rep-line-bool", "repair", [("line_no", True)], ("line_no", "wrong_type")),
    ("rep-line-str", "repair", [("line_no", "143")], ("line_no", "wrong_type")),
    ("rep-len-neg", "repair", [("fragment_len", -1)], ("fragment_len", "out_of_range")),
    ("rep-len-bool", "repair", [("fragment_len", True)], ("fragment_len", "wrong_type")),
    ("rep-len-mismatch", "repair", [("fragment_len", 43)], ("fragment_b64", "out_of_range")),
    ("rep-len-over-cap", "repair", [("fragment_len", 2 * 1024 * 1024), ("fragment_truncated", True)], ("fragment_b64", "out_of_range")),
    ("rep-b64-bad", "repair", [("fragment_b64", "!!!!")], ("fragment_b64", "invalid_format")),
    ("rep-b64-ws", "repair", [("fragment_b64", "eHh4\n")], ("fragment_b64", "invalid_format")),
    ("rep-b64-noncanon", "repair", [("fragment_b64", "eB=="), ("fragment_len", 1)], ("fragment_b64", "invalid_format")),
    ("rep-b64-type", "repair", [("fragment_b64", None)], ("fragment_b64", "wrong_type")),
    ("rep-trunc-type", "repair", [("fragment_truncated", 0)], ("fragment_truncated", "wrong_type")),
    ("rep-trunc-flag", "repair", [("fragment_truncated", True)], ("fragment_truncated", "out_of_range")),
    ("rep-reason", "repair", [("reason", "other")], ("reason", "out_of_range")),
    ("rep-action", "repair", [("action", "truncated")], ("action", "out_of_range")),
    ("rep-action-type", "repair", [("action", None)], ("action", "wrong_type")),
    ("rep-act-newline-vs-json", "repair", [("reason", "invalid_json_last_line")], ("action", "out_of_range")),
    ("rep-act-none-vs-incomplete", "repair", [("action", "none_needed_already_terminated")], ("action", "out_of_range")),
    ("rep-act-none-vs-missing", "repair", [("reason", "missing_trailing_newline"), ("action", "none_needed_already_terminated")], ("action", "out_of_range")),
    # termination
    ("term-source", "term", [("source", "collector")], ("source", "out_of_range")),
    ("term-source-type", "term", [("source", 1)], ("source", "wrong_type")),
    ("term-observed-bad", "term", [("observed_time", "2025-01-01T12:17:03Z")], ("observed_time", "invalid_format")),
    ("term-observed-type", "term", [("observed_time", None)], ("observed_time", "wrong_type")),
    ("term-state-type", "term", [("container_state", "exited")], ("container_state", "wrong_type")),
    ("term-state-null", "term", [("container_state", None)], ("container_state", "wrong_type")),
    ("term-status-type", "term", [("container_state.status", 5)], ("container_state.status", "wrong_type")),
    ("term-exit-str", "term", [("container_state.exit_code", "137")], ("container_state.exit_code", "wrong_type")),
    ("term-exit-bool", "term", [("container_state.exit_code", True)], ("container_state.exit_code", "wrong_type")),
    ("term-exit-float", "term", [("container_state.exit_code", 137.0)], ("container_state.exit_code", "wrong_type")),
    ("term-exit-null", "term", [("container_state.exit_code", None)], ("container_state.exit_code", "wrong_type")),
    ("term-oom-int", "term", [("container_state.oom_killed", 1)], ("container_state.oom_killed", "wrong_type")),
    ("term-label", "term", [("cause_label", "oom")], ("cause_label", "out_of_range")),
    ("term-label-type", "term", [("cause_label", None)], ("cause_label", "wrong_type")),
    ("term-note-type", "term", [("note", 5)], ("note", "wrong_type")),
]


@pytest.mark.parametrize("edits,expected", [c[2:] for c in CASES], ids=[c[0] for c in CASES])
def test_a_wrong_payload_is_rejected_with_the_specific_field_and_reason_code(edits: list, expected: tuple[str, str], request: pytest.FixtureRequest) -> None:
    name = next(c[1] for c in CASES if c[0] == request.node.callspec.id)
    assert found(make(name, *edits)) == {expected}


# --- accepted variants ---------------------------------------------------------------------------------------------

VALID_VARIANTS: list[tuple[str, str, list[tuple[str, object]]]] = [
    ("meta-resumed-process", "meta", [("event", "resumed"), ("durability", "process"), ("fsync_before_ack", False)]),
    ("meta-empty-version", "meta", [("collector_version", "")]),
    ("disc-single", "disc", [("last_missing_seq", 13), ("missing_count", 1), ("detected_at_seq", 14)]),
    ("disc-from-zero", "disc", [("first_missing_seq", 0), ("last_missing_seq", 4), ("missing_count", 5)]),
    ("disc-no-with", "disc", [("reconciliation", "none"), ("reconciles_with", [])]),
    ("disc-partial", "disc", [("reconciliation", "partial_overlap")]),
    ("drop-known-no-note", "known", []),
    ("drop-known-note", "known", [("range_note", "derived")]),
    ("drop-uncertain", "unknown", [("range_status", "uncertain")]),
    ("drop-unknown-no-note", "unknown", [("range_note", DEL)]),
    ("drop-count-max", "known", [("dropped_count", 4_294_967_295), ("detected_at_seq", 4_294_967_308), ("last_missing_seq", 4_294_967_307), ("first_missing_seq", 13)]),
    ("drop-single", "known", [("first_missing_seq", 15), ("dropped_count", 1)]),
    ("anom-reset", "anomaly", [("anomaly", "reset"), ("previous_seq", 100)]),
    ("rej-null-run", "reject", []),
    ("rej-with-run", "reject", [("run_id", "r1")]),
    ("rej-413", "reject", [("http_status", 413), ("reason_code", "body_too_large")]),
    ("rej-empty-preview", "reject", [("body_preview_b64", ""), ("body_len", 0)]),
    ("rej-1024", "reject", [("body_preview_b64", b64(b"x" * 1024)), ("body_len", 5000)]),
    ("rep-null-run", "repair", [("run_id", None), ("file", "_rejections.jsonl")]),
    ("rep-terminated", "repair", [("reason", "invalid_json_last_line"), ("action", "none_needed_already_terminated")]),
    ("rep-missing-newline", "repair", [("reason", "missing_trailing_newline")]),
    ("rep-empty", "repair", [("fragment_len", 0), ("fragment_b64", ""), ("reason", "invalid_json_last_line"), ("action", "none_needed_already_terminated")]),
    ("rep-truncated", "repair", [("fragment_len", 2 * 1024 * 1024), ("fragment_b64", b64(b"y" * 1024 * 1024)), ("fragment_truncated", True)]),
    ("rep-exactly-cap", "repair", [("fragment_len", 1024 * 1024), ("fragment_b64", b64(b"y" * 1024 * 1024))]),
    ("rep-cap-plus-one", "repair", [("fragment_len", 1024 * 1024 + 1), ("fragment_b64", b64(b"y" * 1024 * 1024)), ("fragment_truncated", True)]),
    ("term-sigkill", "term", [("cause_label", "sigkill_sent_by_runner"), ("container_state.oom_killed", False)]),
    ("term-controlled", "term", [("cause_label", "controlled_fatal_process_exit"), ("container_state.exit_code", 1)]),
    ("term-unknown", "term", [("cause_label", "unknown"), ("container_state.exit_code", -1)]),
    ("term-big-exit", "term", [("container_state.exit_code", 2**70)]),
]


@pytest.mark.parametrize("name,edits", [v[1:] for v in VALID_VARIANTS], ids=[v[0] for v in VALID_VARIANTS])
def test_valid_variants_are_accepted(name: str, edits: list) -> None:
    assert validate_record(make(name, *edits)) == []


# --- gap types ---------------------------------------------------------------------------------------------------------


def test_the_gap_types_are_exactly_the_three_designed_ones() -> None:
    assert set(schema.GAP_TYPES) == {"sequence_discontinuity", "silence_timeout", "probe_reported_drop"} and len(schema.GAP_TYPES) == 3
    assert {EXAMPLES[n]["gap_type"] for n in ("disc", "known", "silence")} == set(schema.GAP_TYPES)


def test_an_unusable_gap_type_reports_that_and_not_a_flood_of_unknown_fields() -> None:
    assert found(make("disc", ("gap_type", "mystery"))) == {("gap_type", "out_of_range")}
    assert found(make("disc", ("gap_type", ["x"]))) == {("gap_type", "wrong_type")}
    assert found(make("disc", ("gap_type", DEL))) == {("gap_type", "missing_field")}


def test_the_fields_of_each_gap_type_are_not_interchangeable() -> None:
    disc_as_silence = make("disc", ("gap_type", "silence_timeout"))
    got = found(disc_as_silence)
    assert ("last_sample_time", "missing_field") in got and ("first_missing_seq", "unknown_field") in got
    drop_as_disc = make("known", ("gap_type", "sequence_discontinuity"))
    assert ("missing_count", "missing_field") in found(drop_as_disc) and ("dropped_count", "unknown_field") in found(drop_as_disc)


def test_the_drop_range_keys_may_not_be_absent() -> None:
    assert found(make("unknown", ("first_missing_seq", DEL), ("last_missing_seq", DEL))) == {
        ("first_missing_seq", "missing_field"), ("last_missing_seq", "missing_field")}


def test_a_gap_without_a_usable_range_does_not_raise_on_arithmetic() -> None:
    for first, last, count in [(None, None, None), ("a", 1, 1), (True, 2, 2), (1, 1.5, 1), (2**70, 2**70, 1), (5, 5, "x")]:
        record = make("disc", ("first_missing_seq", first), ("last_missing_seq", last), ("missing_count", count))
        assert isinstance(validate_record(record), list)


# --- envelope and payload together --------------------------------------------------------------------------------------


def test_envelope_and_payload_problems_are_both_reported() -> None:
    record = make("meta", ("schema_version", 2), ("durability", "fast"))
    assert found(record) == {("schema_version", "out_of_range"), ("durability", "out_of_range")}


def test_a_null_run_id_is_still_only_for_rejection_and_repair() -> None:
    assert found(make("meta", ("run_id", None))) == {("run_id", "invalid_run_id")}
    assert found(make("term", ("run_id", None))) == {("run_id", "invalid_run_id")}


def test_the_payload_of_a_record_with_an_unusable_type_is_not_checked() -> None:
    assert found(make("meta", ("record_type", "mystery"))) == {("record_type", "out_of_range")}


def test_non_dict_records_are_one_issue() -> None:
    for bad in (None, 5, "x", [1], (1,), b"x"):
        assert found(bad) == {("$", "wrong_type")}


# --- determinism, non-raising, purity ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_issues_are_deterministic_and_independent_of_key_order(name: str) -> None:
    record = make(name, ("zz", 1), ("aa", 2))
    first = validate_record(record)
    assert first == validate_record(record) == validate_record(dict(reversed(list(record.items()))))
    assert first == validate_record(json.loads(json.dumps(record)))


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_validation_does_not_modify_its_input(name: str) -> None:
    record = make(name, ("zz", 1))
    before = copy.deepcopy(record)
    validate_record(record)
    assert record == before


JSON_ANY = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(-(2**70), 2**70), st.floats(allow_nan=True, allow_infinity=True), st.text(max_size=30)),
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=6), inner, max_size=3)),
    max_leaves=8,
)


@settings(max_examples=150, deadline=None, derandomize=True, database=None)
@given(st.sampled_from(sorted(EXAMPLES)), st.data(), JSON_ANY)
def test_any_value_in_any_field_never_raises_and_gives_stable_sorted_issues(name: str, data: st.DataObject, value: object) -> None:
    record = make(name)
    fields = list(record) + ["zz_extra"]
    record[data.draw(st.sampled_from(fields))] = value
    first = validate_record(record)
    assert first == validate_record(dict(reversed(list(record.items()))))
    assert all(isinstance(i.field, str) and i.reason_code in schema.REASON_CODES for i in first)


@settings(max_examples=100, deadline=None, derandomize=True, database=None)
@given(st.sampled_from(sorted(EXAMPLES)), st.dictionaries(st.one_of(st.text(max_size=5), st.integers()), JSON_ANY, max_size=4))
def test_arbitrary_extra_keys_never_raise_and_are_reported_unknown(name: str, extra: dict) -> None:
    record = {**make(name), **{k: v for k, v in extra.items() if k not in EXAMPLES[name]}}
    issues = validate_record(record)
    unknown = {i.field for i in issues if i.reason_code == "unknown_field"}
    assert unknown == {k if isinstance(k, str) else repr(k) for k in extra if k not in EXAMPLES[name]}


# --- reader level: quarantine before summarize ----------------------------------------------------------------------------


def sample_line(seq: int) -> dict:
    body = {**SAMPLE, "run_id": RUN, "seq": seq}
    return {"record_type": "sample", "schema_version": 1, "run_id": RUN, "recv_time": RECV, "sample": body}


def raw(*records: dict) -> bytes:
    return b"".join(serialize_record(r) for r in records)


BAD_GAPS = {
    "range": make("disc", ("first_missing_seq", 20)),
    "count": make("disc", ("missing_count", 9)),
    "known-null": make("known", ("first_missing_seq", None)),
    "sample-count": make("silence", ("sample_count", 4)),
    "unknown-type": make("disc", ("gap_type", "mystery")),
}


@pytest.mark.parametrize("bad", sorted(BAD_GAPS))
def test_a_malformed_gap_is_quarantined_by_read_evidence_and_summarize_never_raises(bad: str, tmp_path: Path) -> None:
    good_gap = make("disc", ("reconciliation", "none"), ("reconciles_with", []))
    data = raw(sample_line(0), good_gap) + raw(BAD_GAPS[bad]) + raw(sample_line(1))
    path = tmp_path / f"{RUN}.jsonl"
    path.write_bytes(data)
    result = read_evidence(path)
    assert [(e.line_no, e.kind) for e in result.errors] == [(3, "invalid_record")]
    assert result.line_numbers == [1, 2, 4] and [r["record_type"] for r in result.records] == ["sample", "gap", "sample"]
    summary = summarize(result)  # does not raise GapRecordError
    assert (summary.sample_count, summary.gap_count, summary.missing_sample_count, summary.malformed_line_count) == (2, 1, 3, 1)
    assert path.read_bytes() == data  # the evidence file is untouched


def test_a_hand_built_result_with_a_malformed_gap_still_fails_closed() -> None:
    from coroner.evidence.reader import ReadResult

    with pytest.raises(GapRecordError):
        summarize(ReadResult(path="x", records=[BAD_GAPS["range"]], line_numbers=[1], errors=[]))


def test_quarantine_reports_the_field_and_reason_code_in_the_detail() -> None:
    result = read_evidence(raw(BAD_GAPS["count"]), name=f"{RUN}.jsonl")
    assert result.records == [] and "missing_count: out_of_range" in result.errors[0].detail


@pytest.mark.parametrize("name", ["meta", "term", "anomaly", "silence"])
def test_every_bad_payload_type_is_quarantined_and_valid_neighbours_are_kept(name: str) -> None:
    good = make(name, ("run_id", "r1")) if name != "anomaly" else make(name)
    if name == "meta":
        good["evidence_file"] = "r1.jsonl"  # D31: the named file is the run's own
    bad = {**good, "zz_extra": 1}
    data = raw(good, bad, good)
    result = read_evidence(data, name="r1.jsonl")
    assert [(e.line_no, e.kind) for e in result.errors] == [(2, "invalid_record")] and result.line_numbers == [1, 3]


def test_a_repair_record_with_a_wrong_payload_links_nothing() -> None:
    bad = make("repair", ("run_id", "r1"), ("file", "r1.jsonl"), ("line_no", 1), ("fragment_len", 99))
    result = read_evidence(b'{"half\n' + raw(bad), name="r1.jsonl")
    assert [(e.line_no, e.kind, e.has_repair_record) for e in result.errors] == [(1, "invalid_json", False), (2, "invalid_record", False)]


def test_null_run_id_records_still_depend_on_the_file_context() -> None:
    rej, rep = make("reject"), make("repair", ("run_id", None), ("file", "_rejections.jsonl"))
    assert read_evidence(raw(rej, rep), name="_rejections.jsonl").errors == []
    assert [e.kind for e in read_evidence(raw(rej), name="r1.jsonl").errors] == ["invalid_record"]
    assert [e.kind for e in read_evidence(raw(rep), name="r1.jsonl").errors] == ["invalid_record"]
    assert read_evidence(raw(rej)).errors == []  # no name: a null rejection is accepted, as before


# --- real writer and real repair output validate ---------------------------------------------------------------------------


def test_records_written_by_the_repair_library_validate_in_every_mode(tmp_path: Path) -> None:
    w = EvidenceWriter(tmp_path, Durability.PROCESS)
    now = lambda: "2025-01-01T12:05:00.004Z"  # noqa: E731
    cases = {
        "r1.jsonl": b'{"record_type":"sample","sche',  # unterminated, invalid
        "r2.jsonl": serialize_record(sample_line(0)).rstrip(b"\n"),  # unterminated, valid JSON
        "r3.jsonl": b"garbage\n",  # terminated, invalid
        "r4.jsonl": b"\xff\xfe" * 10,  # not UTF-8
        "_rejections.jsonl": b"{",  # null run_id
        "r5.jsonl": b"x" * (repair.FRAGMENT_CAP + 10),  # longer than the 1 MiB cap
    }
    for name, content in cases.items():
        (tmp_path / name).write_bytes(content)
        record = repair.repair_file(tmp_path / name, w, now)
        assert record is not None and validate_record(record) == [], (name, validate_record(record))
        assert all(validate_record(json.loads(line)) == [] for line in (tmp_path / name).read_bytes().splitlines()[1:]), name
    assert record["fragment_truncated"] is True and len(base64.b64decode(record["fragment_b64"])) == repair.FRAGMENT_CAP


def test_a_second_repair_over_a_marker_still_validates(tmp_path: Path) -> None:
    w = EvidenceWriter(tmp_path, Durability.PROCESS)
    path = tmp_path / "r1.jsonl"
    path.write_bytes(b"abc\x00")  # an earlier attempt persisted only its marker
    record = repair.repair_file(path, w, lambda: "2025-01-01T12:05:00.004Z")
    assert validate_record(record) == [] and record["fragment_len"] == 4


def test_the_writer_accepts_valid_payloads_and_refuses_each_invalid_one_without_writing(tmp_path: Path) -> None:
    w = EvidenceWriter(tmp_path, Durability.PROCESS)
    for name in ("meta", "disc", "known", "silence", "anomaly", "term"):
        record = make(name, ("run_id", "r1")) if name != "anomaly" else make(name)
        if name == "meta":
            record["evidence_file"] = "r1.jsonl"
        assert w.append("r1", [record]).record_count == 1
    size = (tmp_path / "r1.jsonl").stat().st_size
    for bad in (make("meta", ("run_id", "r1"), ("event", "x")), make("disc", ("run_id", "r1"), ("missing_count", 1))):
        with pytest.raises(RecordError, match="invalid"):
            w.append("r1", [bad])
    assert (tmp_path / "r1.jsonl").stat().st_size == size
    w.append("_rejections", [make("reject")])
    with pytest.raises(RecordError):
        w.append("_rejections", [make("reject", ("body_len", -1))])


def test_the_serialized_examples_round_trip_through_the_reader_as_valid(tmp_path: Path) -> None:
    records = [make("meta", ("run_id", "r1"), ("evidence_file", "r1.jsonl")), make("disc", ("run_id", "r1")), make("known", ("run_id", "r1")),
               make("silence", ("run_id", "r1")), make("anomaly"), make("term", ("run_id", "r1"))]
    result = read_evidence(raw(*records), name="r1.jsonl")
    assert result.errors == [] and result.records == records


# --- Task 2.19 corrections (design D31) ---------------------------------------------------------------------------------------

DESIGN = Path(__file__).resolve().parents[3] / ".kiro" / "specs" / "coroner-m0" / "design.md"


def _design_records() -> list[dict]:
    text = DESIGN.read_text(encoding="utf-8")
    section = text[text.index("## Data Models"): text.index("### Run_Manifest")]
    return [json.loads(line) for line in section.splitlines() if line.startswith('{"record_type"')]


def test_the_design_examples_are_valid_and_sit_in_the_right_file_context(tmp_path: Path) -> None:
    records = _design_records()
    assert {r["record_type"] for r in records} == set(schema.RECORD_TYPES)
    for record in records:
        assert validate_record(record) == [], record["record_type"]
        name = "_rejections.jsonl" if record["run_id"] is None else f"{record['run_id']}.jsonl"
        if record["record_type"] == "repair":
            name = record["file"]
        result = read_evidence(raw(record), name=name)
        assert result.errors == [], (record["record_type"], result.errors)
    rejection = next(r for r in records if r["record_type"] == "rejection")
    preview = base64.b64decode(rejection["body_preview_b64"], validate=True)
    assert len(preview) == rejection["body_len"] <= 1024 and json.loads(preview)["run_id"] == "a-20250101T120000Z-9f3a1c2e" + "x" * 38
    repaired = next(r for r in records if r["record_type"] == "repair")
    assert len(base64.b64decode(repaired["fragment_b64"], validate=True)) == repaired["fragment_len"] and not repaired["fragment_truncated"]


@pytest.mark.parametrize("status,ok", [(99, False), (100, False), (200, False), (399, False), (400, True), (413, True), (422, True), (500, True), (599, True), (600, False)])
def test_rejection_http_status_is_an_error_status(status: int, ok: bool) -> None:
    assert (found(make("reject", ("http_status", status))) == set()) is ok
    if not ok:
        assert found(make("reject", ("http_status", status))) == {("http_status", "out_of_range")}


def test_rejection_field_may_be_empty_and_reason_may_not() -> None:
    assert found(make("reject", ("field", ""))) == set()
    assert found(make("reject", ("reason", ""))) == {("reason", "out_of_range")}


@pytest.mark.parametrize("code", ["invalid_json", "invalid_run_id", "missing_field", "wrong_type", "out_of_range", "invalid_format",
                                  "null_without_reason", "unknown_field", "body_too_large"])
def test_every_documented_rejection_reason_code_is_accepted(code: str) -> None:
    assert found(make("reject", ("reason_code", code))) == set()


def test_the_rejection_code_set_is_exactly_the_documented_one() -> None:
    assert set(schema.REJECTION_REASON_CODES) == {"invalid_json", "invalid_run_id", "missing_field", "wrong_type", "out_of_range",
                                                  "invalid_format", "null_without_reason", "unknown_field", "body_too_large"}
    assert set(schema.REASON_CODES) <= set(schema.REJECTION_REASON_CODES)


def _known(detected: int, dropped: int, first: int, last: int) -> dict:
    return make("known", ("detected_at_seq", detected), ("dropped_count", dropped), ("first_missing_seq", first), ("last_missing_seq", last))


@pytest.mark.parametrize("args,expected", [
    ((16, 3, 13, 15), set()),
    ((1, 1, 0, 0), set()),  # the earliest possible drop: first_missing_seq is 0
    ((5, 5, 0, 4), set()),
    ((16, 3, 12, 15), {("first_missing_seq", "out_of_range")}),  # first off by one, low
    ((16, 3, 14, 15), {("first_missing_seq", "out_of_range")}),  # first off by one, high
    ((16, 3, 13, 14), {("last_missing_seq", "out_of_range")}),  # last off by one, low
    ((16, 3, 13, 16), {("last_missing_seq", "out_of_range")}),  # last off by one, high
    ((16, 4, 13, 15), {("first_missing_seq", "out_of_range")}),  # dropped_count disagrees with the range
    ((16, 2, 13, 15), {("first_missing_seq", "out_of_range")}),
    ((17, 3, 13, 15), {("first_missing_seq", "out_of_range"), ("last_missing_seq", "out_of_range")}),  # detected_at_seq moved
    ((3, 4, -1, 2), {("first_missing_seq", "out_of_range")}),  # the derived start would be below 0
], ids=["ok", "at-zero", "all", "first-lo", "first-hi", "last-lo", "last-hi", "count+1", "count-1", "at+1", "neg"])
def test_a_known_drop_range_follows_from_detected_at_seq_and_dropped_count(args: tuple, expected: set) -> None:
    assert found(_known(*args)) == expected


@pytest.mark.parametrize("status", ["unknown", "uncertain"])
def test_an_unknown_or_uncertain_drop_keeps_a_null_range_whatever_the_counts(status: str) -> None:
    record = make("unknown", ("range_status", status), ("detected_at_seq", 3), ("dropped_count", 9))
    assert found(record) == set()


@pytest.mark.parametrize("anomaly,seq,previous,ok", [
    ("duplicate", 7, 9, True), ("duplicate", 9, 9, True), ("duplicate", 0, 0, True), ("duplicate", 10, 9, False),
    ("reset", 7, 9, True), ("reset", 0, 1, True), ("reset", 9, 9, False), ("reset", 10, 9, False),
], ids=["dup-below", "dup-equal", "dup-zero", "dup-above", "reset-below", "reset-zero", "reset-equal", "reset-above"])
def test_anomaly_seq_is_at_most_previous_for_a_duplicate_and_below_it_for_a_reset(anomaly: str, seq: int, previous: int, ok: bool) -> None:
    record = make("anomaly", ("anomaly", anomaly), ("seq", seq), ("previous_seq", previous), ("sample.seq", seq))
    assert found(record) == (set() if ok else {("seq", "out_of_range")})


@pytest.mark.parametrize("body_len,preview,ok", [
    (2, b"xyz", False), (3, b"xyz", True), (4, b"xyz", True), (0, b"", True), (1023, b"x" * 1024, False), (1024, b"x" * 1024, True),
    (100_000, b"x" * 1024, True),
], ids=["lt", "eq", "gt", "zero", "lt-1024", "eq-1024", "413"])
def test_rejection_body_len_is_at_least_the_decoded_preview(body_len: int, preview: bytes, ok: bool) -> None:
    record = make("reject", ("body_len", body_len), ("body_preview_b64", b64(preview)))
    assert found(record) == (set() if ok else {("body_len", "out_of_range")})


@pytest.mark.parametrize("name,edits,expected", [
    ("meta", [("evidence_file", "r1.jsonl")], {("evidence_file", "out_of_range")}),  # a well-formed name of another run
    ("meta", [("run_id", "r1")], {("evidence_file", "out_of_range")}),
    ("meta", [("run_id", "r1"), ("evidence_file", "r1.jsonl")], set()),
    ("meta", [("run_id", "r1"), ("evidence_file", "R1.jsonl")], {("evidence_file", "out_of_range")}),
    ("repair", [("file", "r1.jsonl")], {("file", "out_of_range")}),  # valid run_id, other run's file
    ("repair", [("file", "_rejections.jsonl")], {("file", "out_of_range")}),  # valid run_id, rejections file
    ("repair", [("run_id", None)], {("file", "out_of_range")}),  # null run_id, a run file
    ("repair", [("run_id", None), ("file", "other.jsonl")], {("file", "out_of_range")}),
    ("repair", [("run_id", None), ("file", "_rejections.jsonl")], set()),
    ("repair", [("run_id", "r1"), ("file", "r1.jsonl")], set()),
], ids=["m-other", "m-runid", "m-ok", "m-case", "r-other", "r-rej", "r-null-run", "r-null-other", "r-null-ok", "r-ok"])
def test_the_file_a_record_names_is_the_file_of_its_run_id(name: str, edits: list, expected: set) -> None:
    assert found(make(name, *edits)) == expected


def test_a_gap_may_end_before_it_starts_and_gap_ids_are_not_checked_for_uniqueness() -> None:
    """D31: no start_time <= end_time rule and no run-level uniqueness rule in the schema."""
    backwards = make("disc", ("start_time", "2025-01-01T12:00:09.000Z"), ("end_time", "2025-01-01T12:00:08.000Z"))
    assert validate_record(backwards) == []
    result = read_evidence(raw(backwards, backwards), name=f"{RUN}.jsonl")
    assert result.errors == [] and len(result.records) == 2


def test_issue_order_is_stable_when_a_known_drop_has_several_problems() -> None:
    issues = validate_record(_known(17, 3, 13, 15))
    assert [i.field for i in issues] == ["first_missing_seq", "last_missing_seq"]
    assert issues == validate_record(_known(17, 3, 13, 15))


@pytest.mark.parametrize("bad", ["drop-first", "anomaly-dup", "anomaly-reset", "rej-status", "rej-len", "meta-file", "repair-file"])
def test_records_that_break_a_d31_rule_are_quarantined_and_nothing_reaches_the_summary(bad: str, tmp_path: Path) -> None:
    records = {
        "drop-first": make("known", ("first_missing_seq", 12)),
        "anomaly-dup": make("anomaly", ("seq", 10), ("sample.seq", 10)),
        "anomaly-reset": make("anomaly", ("anomaly", "reset"), ("previous_seq", 7)),
        "rej-status": make("reject", ("http_status", 200)),
        "rej-len": make("reject", ("body_len", 1)),
        "meta-file": make("meta", ("evidence_file", "other.jsonl")),
        "repair-file": make("repair", ("file", "other.jsonl")),
    }
    record = records[bad]
    name = "_rejections.jsonl" if record["run_id"] is None else f"{record['run_id']}.jsonl"
    run = record["run_id"]
    samples = [{**sample_line(n), "run_id": run, "sample": {**sample_line(n)["sample"], "run_id": run}} for n in (0, 1)]
    data = raw(samples[0]) + raw(record) + raw(samples[1]) if run is not None else raw(record)
    path = tmp_path / name
    path.write_bytes(data)
    result = read_evidence(path)
    assert [e.kind for e in result.errors] == ["invalid_record"] and result.records == (
        [] if run is None else samples)
    summarize(result)  # never raises
    assert path.read_bytes() == data


def test_the_derivation_applies_only_to_known_drops_and_reports_each_problem_once() -> None:
    issues = validate_record(make("unknown", ("first_missing_seq", 5), ("last_missing_seq", 6)))
    assert [(i.field, i.reason_code) for i in issues] == [("first_missing_seq", "out_of_range"), ("last_missing_seq", "out_of_range")]
