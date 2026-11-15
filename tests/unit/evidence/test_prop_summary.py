"""Property 13: Run summary is consistent with the records (task 2.17).

*For any* set of records for a run, the summary's sample count equals the number of valid `sample` records
(Malformed_Lines excluded), its last sample time equals the latest stored Sample `recv_time` (or the text
`none received` when there are none), it reports the Missing_Sample_Count and the Unreconciled_Drop_Count with
its uncertainty flag, the termination cause is `unknown` when no `termination` record exists, and the
unobserved-interval statement is present whenever the run has a `termination` record or an open silence gap.

**Validates: Requirements 6.5, 6.6, 6.7, 12.5**

Interpretation of "latest stored Sample" (task 2.17, approved): the last valid `sample` record in file order, the
envelope `recv_time` (Receive_Time), not the maximum `recv_time` and not the Sample's own `ts` (Capture_Time).

Independent oracles: the expected summary is computed here from the generated items with plain Python (sets, list
scans, `json`). Missing_Sample_Count is a brute-force set union of the discontinuity ranges; the unreconciled drops
follow the design formula over that set. `summarize` is additionally cross-checked against
`gaps.missing_sample_count` and `gaps.unreconciled_drops`. Files are real: Malformed_Lines are raw junk lines, and
isolated-by-repair lines are valid Samples written without a newline and then repaired by the real `repair_file`
(D30 marker plus repair record). Gap payloads are well formed (a malformed payload raises, tested explicitly).
Stored `reconciliation` values are not predicted, so only the warnings that can be predicted are asserted.
"""
from __future__ import annotations

import copy
import json
import re
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from coroner.evidence.gaps import GapRecordError, missing_sample_count, unreconciled_drops
from coroner.evidence.reader import UNOBSERVED_NOTE, read_evidence, summarize
from coroner.evidence.repair import repair_file
from coroner.evidence.writer import Durability, EvidenceWriter, serialize_record

SETTINGS = dict(max_examples=150, deadline=None, derandomize=True, database=None)
NOW = "2025-01-01T12:05:00.004Z"
RECV_POOL = [f"2025-01-01T12:00:{s:02d}.530Z" for s in (9, 3, 7, 1, 5, 8)]  # unordered on purpose
JUNK = [b"garbage", b"[1]", b"\xff\xfe", b"{", b'"text"', b'{"a":']


# --- record builders --------------------------------------------------------------------------------------


def envelope(record_type: str, **fields: object) -> dict:
    return {"record_type": record_type, "schema_version": 1, "run_id": "r1", "recv_time": RECV_POOL[0], **fields}


def make_sample(seq: int, recv_time: str) -> dict:
    body = {
        "run_id": "r1", "seq": seq, "ts": f"2025-01-01T11:59:{seq % 60:02d}.512Z", "rss_bytes": 71303168, "request_count": 9,
        "loop_delay_ms": 0.84, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }
    return {"record_type": "sample", "schema_version": 1, "run_id": "r1", "recv_time": recv_time, "sample": body}


def build(item: tuple, index: int) -> dict:
    kind = item[0]
    gid = f"gap-{index}"
    if kind == "sample":
        return make_sample(item[1], RECV_POOL[item[2]])
    if kind == "disc":
        _, lo, hi, at = item
        return envelope("gap", gap_id=gid, gap_type="sequence_discontinuity", start_time=RECV_POOL[0], end_time=RECV_POOL[0],
                        first_missing_seq=lo, last_missing_seq=hi,
                        missing_count=hi - lo + 1, reconciliation="none", reconciles_with=[], detected_at_seq=at)
    if kind == "drop":
        _, status, lo, hi, count, at = item
        return envelope("gap", gap_id=gid, gap_type="probe_reported_drop", start_time=RECV_POOL[0], end_time=RECV_POOL[0],
                        dropped_count=count, range_status=status,
                        first_missing_seq=lo, last_missing_seq=hi, reconciliation="none", reconciles_with=[], detected_at_seq=at)
    if kind == "silence":
        return envelope("gap", gap_id=gid, gap_type="silence_timeout", start_time=RECV_POOL[0], end_time=RECV_POOL[0],
                        last_sample_time="2025-01-01T12:00:10.530Z",
                        detected_time="2025-01-01T12:00:12.081Z", threshold_ms=1500, interval_ms=500, sample_count="unknown",
                        reconciliation="not_applicable", reconciles_with=[])
    if kind == "anomaly":
        return envelope("sequence_anomaly", anomaly=item[1], seq=1, previous_seq=2, sample=make_sample(1, RECV_POOL[0])["sample"])
    if kind == "term":
        return envelope("termination", source="experiment_runner", observed_time=RECV_POOL[1], cause_label=item[1],
                        container_state={"status": "exited", "exit_code": 137, "oom_killed": True}, note="Observed after the fact.")
    if kind == "meta":
        return envelope("run_metadata", event="created", durability=item[1], fsync_before_ack=item[1] == "crash",
                        evidence_file="r1.jsonl", collector_version="0.1.0")
    raise AssertionError(item)


VALID_KINDS = ("sample", "disc", "drop", "silence", "anomaly", "term", "meta")


def write_file(directory: Path, items: list[tuple]) -> Path:
    path = directory / "r1.jsonl"
    path.write_bytes(b"")
    writer = None
    for index, item in enumerate(items):
        if item[0] == "junk":
            with path.open("ab") as handle:
                handle.write(item[1] + b"\n")
        elif item[0] == "isolated":
            with path.open("ab") as handle:  # a valid Sample that was never acknowledged: no newline
                handle.write(serialize_record(make_sample(item[1], RECV_POOL[item[2]])).rstrip(b"\n"))
            writer = writer or EvidenceWriter(directory, Durability.PROCESS)
            assert repair_file(path, writer, lambda: NOW)["action"] == "appended_newline"
        else:
            with path.open("ab") as handle:
                handle.write(serialize_record(build(item, index)))
    return path


# --- strategies ---------------------------------------------------------------------------------------------

seqs = st.integers(0, 30)
recv_ix = st.integers(0, len(RECV_POOL) - 1)
ranges = st.integers(0, 12).flatmap(lambda lo: st.integers(lo, 12).map(lambda hi: (lo, hi)))
ats = st.integers(0, 20)

item_strategy = st.one_of(
    st.tuples(st.just("sample"), seqs, recv_ix),
    st.tuples(st.just("junk"), st.sampled_from(JUNK)),
    st.tuples(st.just("isolated"), seqs, recv_ix),
    st.builds(lambda r, a: ("disc", r[0], r[1], a), ranges, ats),
    # D31 (5): a known range is derived from the Sample that reported it: [at - count, at - 1]
    st.integers(1, 20).flatmap(lambda a: st.integers(1, min(a, 6)).map(lambda c: ("drop", "known", a - c, a - 1, c, a))),
    st.builds(lambda s, c, a: ("drop", s, None, None, c, a), st.sampled_from(["unknown", "uncertain"]), st.integers(1, 6), ats),
    st.tuples(st.just("silence")),
    st.tuples(st.just("anomaly"), st.sampled_from(["duplicate", "reset"])),
    st.tuples(st.just("term"), st.sampled_from(["unknown", "docker_oom_kill", "sigkill_sent_by_runner"])),
    st.tuples(st.just("meta"), st.sampled_from(["process", "crash"])),
)
item_lists = st.lists(item_strategy, max_size=14)


# --- the oracle ---------------------------------------------------------------------------------------------


def expected(items: list[tuple]) -> dict:
    valid = [i for i in items if i[0] in VALID_KINDS]
    samples = [i for i in valid if i[0] == "sample"]
    covered: set[int] = set()
    for i in valid:
        if i[0] == "disc":
            covered |= set(range(i[1], i[2] + 1))
    unrec_count, unrec_flag = 0, False
    for i in valid:
        if i[0] != "drop":
            continue
        contribution = len(set(range(i[2], i[3] + 1)) - covered) if i[1] == "known" else i[4]
        unrec_count += contribution
        if contribution > 0 or i[1] != "known":
            unrec_flag = True
    last_sample_index = max((n for n, i in enumerate(valid) if i[0] == "sample"), default=-1)
    open_silence = any(i[0] == "silence" and n > last_sample_index for n, i in enumerate(valid))
    terms = [i for i in valid if i[0] == "term"]
    types = {"disc": "sequence_discontinuity", "drop": "probe_reported_drop", "silence": "silence_timeout"}
    by_type: dict[str, int] = {}
    for i in valid:
        if i[0] in types:
            by_type[types[i[0]]] = by_type.get(types[i[0]], 0) + 1
    levels: list[str] = []
    for i in valid:
        if i[0] == "meta" and i[1] not in levels:
            levels.append(i[1])
    return {
        "sample_count": len(samples),
        "last": RECV_POOL[samples[-1][2]] if samples else "none received",
        "missing": len(covered),
        "unrec": unrec_count,
        "flag": unrec_flag,
        "malformed": sum(1 for i in items if i[0] in ("junk", "isolated")),
        "isolated": sum(1 for i in items if i[0] == "isolated"),
        "gap_count": sum(by_type.values()),
        "by_type": by_type,
        "dups": sum(1 for i in valid if i[0] == "anomaly" and i[1] == "duplicate"),
        "resets": sum(1 for i in valid if i[0] == "anomaly" and i[1] == "reset"),
        "term": terms[-1] if terms else None,
        "n_terms": len(terms),
        "note": bool(terms) or open_silence,
        "silences": sum(1 for i in valid if i[0] == "silence"),
        "levels": levels,
        "run_id": "r1" if any(i[0] in VALID_KINDS or i[0] == "isolated" for i in items) else None,
    }


def check(items: list[tuple]) -> None:
    exp = expected(items)
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(Path(tmp), items)
        file_bytes = path.read_bytes()
        result = read_evidence(path)
        before = copy.deepcopy(result)
        s = summarize(result)
        assert result == before and path.read_bytes() == file_bytes  # non-mutation
        assert summarize(result) == s  # determinism
        assert summarize(read_evidence(file_bytes, name="r1.jsonl")) == s  # Path and bytes agree

    assert s.run_id == exp["run_id"]
    assert s.sample_count == exp["sample_count"]
    assert s.last_sample_time == exp["last"]
    assert s.missing_sample_count == exp["missing"]
    assert (s.unreconciled_drop_count, s.unreconciled_drop_uncertain) == (exp["unrec"], exp["flag"])
    assert s.malformed_line_count == exp["malformed"] == len(s.read_errors)
    assert sum(1 for e in s.read_errors if e.kind == "isolated_by_repair") == exp["isolated"]
    assert s.gap_count == exp["gap_count"] and s.gap_counts_by_type == exp["by_type"]
    assert (s.duplicate_count, s.reset_count, s.rejection_count) == (exp["dups"], exp["resets"], 0)
    assert len(s.silence_intervals) == exp["silences"]
    assert all(x["sample_count"] == "unknown" for x in s.silence_intervals)
    assert s.durability_levels == exp["levels"]
    assert (s.unobserved_interval_note is not None) == exp["note"]
    if exp["note"]:
        assert s.unobserved_interval_note == UNOBSERVED_NOTE and not re.search(r"\d", s.unobserved_interval_note)
    term = exp["term"]
    if term is None:
        assert s.termination is None and s.termination_cause == "unknown"
    else:
        stored = json.loads(serialize_record(build(term, 0)))
        assert s.termination == stored  # nothing added: no duration, no inferred field
        assert s.termination_cause == term[1]
    assert sum("termination records found" in w for w in s.warnings) == (1 if exp["n_terms"] > 1 else 0)
    assert not any("unrecognised gap_type" in w for w in s.warnings)

    # cross-check against the approved gap-accounting functions, on the records the reader kept
    gaps = [r for r in result.records if r["record_type"] == "gap"]
    assert s.missing_sample_count == missing_sample_count(gaps)
    drops = unreconciled_drops(gaps)
    assert s.unreconciled_drop_count == sum(d["count"] for d in drops) and s.unreconciled_drops == drops


@settings(**SETTINGS)
@given(item_lists)
@example([])
@example([("junk", b"garbage"), ("junk", b"[1]")])
@example([("isolated", 4, 0)])
@example([("sample", 0, 0), ("sample", 1, 3)])  # file order and max recv_time disagree: expect RECV_POOL[3]
@example([("disc", 5, 9, 10), ("drop", "known", 4, 9, 6, 10), ("drop", "unknown", None, None, 1, 11)])
@example([("sample", 0, 1), ("silence",), ("isolated", 1, 2)])  # an isolated Sample does not close the silence
@example([("silence",), ("sample", 2, 1)])  # closed silence, no note
@example([("term", "unknown"), ("term", "docker_oom_kill")])
def test_property_13_summary_is_consistent_with_the_records(items: list[tuple]) -> None:
    check(items)


# --- explicit tests -------------------------------------------------------------------------------------------


def test_last_sample_time_follows_file_order_not_the_maximum_recv_time() -> None:
    s = summarize(read_evidence(serialize_record(make_sample(0, RECV_POOL[0])) + serialize_record(make_sample(1, RECV_POOL[3])), name="r1.jsonl"))
    assert RECV_POOL[0] > RECV_POOL[3] and s.last_sample_time == RECV_POOL[3]


def test_rejections_are_counted_and_malformed_lines_are_not_rejections() -> None:
    rejection = {"record_type": "rejection", "schema_version": 1, "run_id": None, "recv_time": RECV_POOL[0], "http_status": 422, "reason_code": "invalid_json",
                 "field": "$", "reason": "body is not JSON", "body_len": 3, "body_preview_b64": "eHh4"}
    data = serialize_record(rejection) + b"garbage\n" + serialize_record(rejection)
    s = summarize(read_evidence(data, name="_rejections.jsonl"))
    assert (s.rejection_count, s.malformed_line_count, s.sample_count, s.last_sample_time) == (2, 1, 0, "none received")
    assert s.termination_cause == "unknown" and s.unobserved_interval_note is None


def test_malformed_only_evidence_has_no_counts_and_no_run_id() -> None:
    s = summarize(read_evidence(b"garbage\n[1]\n", name="r1.jsonl"))
    assert (s.run_id, s.sample_count, s.malformed_line_count, s.last_sample_time) == (None, 0, 2, "none received")
    assert s.gap_count == 0 and s.unobserved_interval_note is None


def test_a_malformed_gap_payload_is_quarantined_before_summarize_since_task_2_19() -> None:
    bad = envelope("gap", gap_id="gap-1", gap_type="sequence_discontinuity", first_missing_seq=5, last_missing_seq=2)
    s = summarize(read_evidence(serialize_record(bad), name="r1.jsonl"))
    assert (s.gap_count, s.malformed_line_count, s.read_errors[0].kind) == (0, 1, "invalid_record")


def test_a_malformed_gap_in_a_hand_built_result_still_raises_by_the_fail_closed_decision() -> None:
    from coroner.evidence.reader import ReadResult

    bad = envelope("gap", gap_id="gap-1", gap_type="sequence_discontinuity", first_missing_seq=5, last_missing_seq=2)
    with pytest.raises(GapRecordError):
        summarize(ReadResult(path="x", records=[bad], line_numbers=[1], errors=[]))
