"""Task 3.5: IngestService, build_records, GapReplay (design D26, D29, D31; Requirements 4.2 to 4.9, 4.15, 6.1, 6.3, 6.4, 7.5, 7.7).

Everything runs in pytest temporary directories with recording doubles for fsync and the directory fsync, an
injected clock and an injected monotonic clock, so no host crash, Docker or network is needed. Failures are
injected through the writer's `open` seam, as in test_state.py and test_writer.py.
"""
from __future__ import annotations

import base64
import builtins
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as hs

from coroner.collector.config import CollectorConfig
from coroner.collector.service import (
    GapReplay,
    IngestService,
    build_records,
    create_ingest_service,
)
from coroner.collector.state import RunRegistry
from coroner.common.timeutil import format_utc_ms
from coroner.evidence import writer as writer_module
from coroner.evidence.gaps import IntervalSet, SeqState, classify, derive_drop_range
from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.schema import validate_record
from coroner.evidence.writer import Durability, serialize_record

BASE = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TERMINATION = {
    "source": "experiment_runner", "observed_time": "2025-01-01T12:17:03.221Z",
    "container_state": {"status": "exited", "exit_code": 137, "oom_killed": True},
    "cause_label": "docker_oom_kill", "note": "Observed after the fact via docker inspect.",
}


# --- doubles and builders --------------------------------------------------------------------------------


class Clock:
    """A wall clock that advances 10 ms per reading, so every record gets its own `recv_time`."""

    def __init__(self) -> None:
        self.ticks = 0

    def now(self) -> str:
        self.ticks += 1
        return format_utc_ms(BASE + timedelta(milliseconds=10 * self.ticks))


class Rec:
    def __init__(self) -> None:
        self.file_syncs = 0
        self.dir_syncs = 0
        self.fail_file = False

    def fsync(self, fd: int) -> None:
        if self.fail_file:
            raise OSError("fsync failed")
        self.file_syncs += 1

    def fsync_dir(self, path: Path) -> None:
        self.dir_syncs += 1


class FakeProbe:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1


class FailingFile:
    def __init__(self, real: Any, mode: str, limit: int | None = None) -> None:
        self._real, self._mode, self._limit = real, mode, limit

    def __enter__(self) -> "FailingFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        if self._mode == "write":
            raise OSError("disk full")
        if self._mode == "partial":
            self._real.write(data[: self._limit if self._limit is not None else len(data) // 2])
            raise OSError("disk full")
        return self._real.write(data)

    def flush(self) -> None:
        if self._mode == "flush":
            self._real.flush()
            raise OSError("flush failed")
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()


class Faults:
    """Arm a failure for the next `remaining` opens of the file called `name` (after skipping `skip` opens)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.mode: str | None = None
        self.name = ""
        self.remaining = 0
        self.skip = 0
        self.limit: int | None = None
        monkeypatch.setattr(writer_module, "open", self._open, raising=False)

    def arm(self, mode: str, name: str, remaining: int = 1, skip: int = 0, limit: int | None = None) -> None:
        self.mode, self.name, self.remaining, self.skip, self.limit = mode, name, remaining, skip, limit

    def disarm(self) -> None:
        self.mode = None

    def _open(self, path: Any, mode: str) -> Any:
        real = builtins.open(path, mode)
        if self.mode is None or Path(path).name != self.name:
            return real
        if self.skip > 0:
            self.skip -= 1
            return real
        if self.remaining > 0:
            self.remaining -= 1
            return FailingFile(real, self.mode, self.limit)
        return real


def sample(run_id: str = "r1", seq: int = 0, dropped: int = 0, **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 1000, "request_count": 1,
        "loop_delay_ms": 0.5, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": dropped,
    }
    out.update(over)
    return out


def body(obj: object) -> bytes:
    return json.dumps(obj).encode()


class Env:
    def __init__(self, tmp_path: Path, level: Durability = Durability.CRASH, max_body: int = 65536, monotonic: Any = None) -> None:
        self.directory = tmp_path / "evidence"
        self.config = CollectorConfig(durability=level, max_body_bytes=max_body, watchdog_tick_ms=50, evidence_dir=self.directory)
        self.rec = Rec()
        self.probe = FakeProbe()
        self.clock = Clock()
        self.monotonic = monotonic
        self.service = self._build()
        self.service.registry.start()

    def _build(self) -> IngestService:
        kwargs: dict[str, Any] = {} if self.monotonic is None else {"monotonic": self.monotonic}
        return create_ingest_service(
            self.config, fsync=self.rec.fsync, fsync_dir=self.rec.fsync_dir, now=self.clock.now, storage_probe=self.probe, **kwargs
        )

    def restart(self) -> None:
        self.service.registry.close()
        self.service = self._build()
        self.service.registry.start()

    def ingest(self, run_id: str = "r1", seq: int = 0, dropped: int = 0, **over: Any) -> Any:
        return self.service.ingest(body(sample(run_id, seq, dropped, **over)))

    def path(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.jsonl"

    def records(self, run_id: str = "r1") -> list[dict[str, Any]]:
        return list(read_evidence(self.path(run_id)).records)

    def of_type(self, record_type: str, run_id: str = "r1") -> list[dict[str, Any]]:
        return [r for r in self.records(run_id) if r["record_type"] == record_type]

    def rejections(self) -> list[dict[str, Any]]:
        return list(read_evidence(self.directory / "_rejections.jsonl").records)

    def state(self, run_id: str = "r1") -> Any:
        return self.service.registry.get(run_id)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def hand_file(env: Env, run_id: str, *records: dict[str, Any], raw_tail: bytes = b"") -> Path:
    path = env.path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for record in records:
        assert validate_record(record) == [], record
    path.write_bytes(b"".join(serialize_record(r) for r in records) + raw_tail)
    return path


def sample_rec(run_id: str, seq: int, dropped: int = 0, recv: str | None = None) -> dict[str, Any]:
    recv = recv or format_utc_ms(BASE + timedelta(seconds=seq))
    return {"record_type": "sample", "schema_version": 1, "run_id": run_id, "recv_time": recv, "sample": sample(run_id, seq, dropped)}


def gap_rec(run_id: str, gap_id: str, first: int, last: int, detected: int) -> dict[str, Any]:
    return {
        "record_type": "gap", "schema_version": 1, "run_id": run_id, "recv_time": format_utc_ms(BASE), "gap_id": gap_id,
        "gap_type": "sequence_discontinuity", "start_time": format_utc_ms(BASE), "end_time": format_utc_ms(BASE),
        "first_missing_seq": first, "last_missing_seq": last, "missing_count": last - first + 1, "reconciliation": "none",
        "reconciles_with": [], "detected_at_seq": detected,
    }


# --- valid ingestion -------------------------------------------------------------------------------------


def test_valid_ingest_writes_the_sample_and_acknowledges_with_the_real_durability(env: Env) -> None:
    first = env.ingest(seq=0)
    second = env.ingest(seq=1)
    assert (first.http_status, second.http_status) == (200, 200)
    assert first.body == {"status": "stored", "run_id": "r1", "seq": 0, "durability": "crash", "recv_time": first.recv_time}
    records = env.records()
    assert [r["record_type"] for r in records] == ["run_metadata", "sample", "sample"]
    assert records[1]["sample"] == sample(seq=0) and records[1]["recv_time"] == first.recv_time
    assert env.rec.file_syncs >= 3 and read_evidence(env.path("r1")).errors == []
    assert all(validate_record(r) == [] for r in records)


def test_process_durability_is_reported_when_no_fsync_ran(tmp_path: Path) -> None:
    env = Env(tmp_path, Durability.PROCESS)
    result = env.ingest(seq=0)
    assert result.body["durability"] == "process" and env.rec.file_syncs == 0


def test_the_acknowledgement_follows_the_append_and_state_changes_only_after_it(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.ingest(seq=0)
    registry = env.service.registry
    seen_calls: list[tuple[Any, ...]] = []
    original = registry.append

    def spy(state: Any, records: Any) -> Any:
        before = (state.highest_seq, state.seen.ranges(), state.next_gap_number)
        out = original(state, records)
        after_append = (state.highest_seq, state.seen.ranges(), state.next_gap_number)
        seen_calls.append((before, after_append, len(records), env.path("r1").read_bytes().count(b"\n")))
        return out

    monkeypatch.setattr(registry, "append", spy)
    result = env.ingest(seq=3)  # discontinuity: sample and gap in one append
    before, after_append, count, lines = seen_calls[0]
    assert before == after_append == (0, ((0, 0),), 1) and count == 2  # state untouched until the append returned
    assert lines == 4 and result.http_status == 200 and env.state().highest_seq == 3 and env.state().next_gap_number == 2


def test_ingest_ms_is_logged_with_the_injected_clock_and_never_the_body(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ticks = iter([100.0, 100.0125, 200.0, 200.5])
    env = Env(tmp_path, monotonic=lambda: next(ticks))
    secret = "SECRET-PAYLOAD-MARKER"
    with caplog.at_level(logging.INFO, logger="coroner.collector.service"):
        result = env.service.ingest(body(sample(container_id_source="mountinfo")))
        env.service.ingest(b'{"x": "' + secret.encode() + b'"')
    assert result.ingest_ms == pytest.approx(12.5)
    first = [r for r in caplog.records if getattr(r, "ingest_kind", "") == "stored"][0]
    assert first.ingest_ms == pytest.approx(12.5) and "ingest_ms=12.500" in first.getMessage()
    assert env.state().last_sample_monotonic == 100.0  # the watchdog clock is the same injected clock
    assert secret not in caplog.text


# --- rejections ------------------------------------------------------------------------------------------


def test_oversized_body_is_413_and_recorded_in_rejections_with_a_bounded_preview(tmp_path: Path) -> None:
    env = Env(tmp_path, max_body=100)
    raw = b'{"pad":"' + b"x" * 5000 + b'"}'
    result = env.service.ingest(raw)
    assert result.http_status == 413 and result.body["reason_code"] == "body_too_large" and "rejection_recorded" not in result.body
    (rejection,) = env.rejections()
    assert rejection["run_id"] is None and rejection["http_status"] == 413 and rejection["reason_code"] == "body_too_large"
    assert rejection["body_len"] == len(raw) and base64.b64decode(rejection["body_preview_b64"]) == raw[:1024]
    assert not env.path("r1").exists() and validate_record(rejection) == []


@pytest.mark.parametrize(
    "raw",
    [b"{", b"", b"\xff\xfe", b'{"run_id":"r1","seq":NaN}', b'{"run_id":"r1","seq":Infinity}', b'{"run_id":"r1","seq":-Infinity}', pytest.param(b"[" * 60000, id="deep")],
)
def test_invalid_json_is_422_and_goes_to_rejections(env: Env, raw: bytes) -> None:
    result = env.service.ingest(raw)
    assert result.http_status == 422 and result.body["reason_code"] == "invalid_json" and result.body["error"] == "validation_failed"
    (rejection,) = env.rejections()
    assert rejection["run_id"] is None and rejection["reason_code"] == "invalid_json" and rejection["body_len"] == len(raw)
    assert base64.b64decode(rejection["body_preview_b64"]) == raw[:1024]


@pytest.mark.parametrize(
    ("override", "code", "field"),
    [
        ({"seq": "1"}, "wrong_type", "seq"),
        ({"seq": -1}, "out_of_range", "seq"),
        ({"ts": "yesterday"}, "invalid_format", "ts"),
        ({"rss_bytes": None}, "null_without_reason", "rss_bytes"),
        ({"surprise": 1}, "unknown_field", "surprise"),
        ({"loop_delay_ms": 1e999}, "out_of_range", "loop_delay_ms"),
    ],
)
def test_an_invalid_sample_with_a_valid_run_id_is_rejected_in_the_run_file(env: Env, override: dict, code: str, field: str) -> None:
    raw = json.dumps(sample(**{"seq": 0, **override})).replace("Infinity", "1e999").encode()  # json.dumps writes Infinity; 1e999 parses to inf
    result = env.service.ingest(raw)
    assert (result.http_status, result.body["reason_code"], result.body["field"]) == (422, code, field)
    (rejection,) = env.of_type("rejection")
    assert rejection["run_id"] == "r1" and rejection["reason_code"] == code and rejection["field"] == field
    assert env.of_type("sample") == [] and env.state().highest_seq is None
    assert not (env.directory / "_rejections.jsonl").exists()


def test_a_missing_field_is_reported_in_the_run_file(env: Env) -> None:
    obj = sample(seq=0)
    del obj["pid"]
    result = env.service.ingest(body(obj))
    assert result.body["reason_code"] == "missing_field" and result.body["field"] == "pid"
    assert env.of_type("rejection")[0]["reason"]


@pytest.mark.parametrize("bad", ["bad id!", "", "x" * 65, "_rejections", 5, None, "../up"])
def test_an_invalid_run_id_is_rejected_in_rejections_with_a_null_run_id(env: Env, bad: object) -> None:
    result = env.service.ingest(body(sample(run_id=bad)))  # type: ignore[arg-type]
    assert result.http_status == 422 and result.body["reason_code"] == "invalid_run_id"
    (rejection,) = env.rejections()
    assert rejection["run_id"] is None and rejection["field"] == "run_id" and rejection["reason_code"] == "invalid_run_id"
    assert [p.name for p in env.directory.glob("*.jsonl")] == ["_rejections.jsonl"]


def test_a_missing_run_id_and_a_non_object_body_go_to_rejections(env: Env) -> None:
    obj = sample()
    del obj["run_id"]
    assert env.service.ingest(body(obj)).body["reason_code"] == "invalid_run_id"
    non_object = env.service.ingest(b"[1,2]")
    assert (non_object.http_status, non_object.body["reason_code"], non_object.body["field"]) == (422, "wrong_type", "$")
    assert [r["reason_code"] for r in env.rejections()] == ["invalid_run_id", "wrong_type"]


def test_rejected_samples_do_not_change_sequence_state(env: Env) -> None:
    env.ingest(seq=0)
    env.ingest(seq=1, ts="bad")
    assert env.ingest(seq=1).body["status"] == "stored" and env.of_type("gap") == []


# --- sequence classification -----------------------------------------------------------------------------


def test_first_sample_at_zero_has_no_gap_and_a_later_first_sample_is_missing_from_zero(env: Env) -> None:
    env.ingest("a", 0)
    env.ingest("b", 5)
    assert env.of_type("gap", "a") == []
    (gap,) = env.of_type("gap", "b")
    assert (gap["gap_type"], gap["first_missing_seq"], gap["last_missing_seq"], gap["missing_count"], gap["detected_at_seq"]) == (
        "sequence_discontinuity", 0, 4, 5, 5,
    )
    assert gap["start_time"] == gap["end_time"] == gap["recv_time"] == env.of_type("sample", "b")[0]["recv_time"]


def test_consecutive_samples_make_no_gap_and_a_skip_makes_one_inclusive_range(env: Env) -> None:
    for seq in (0, 1, 2):
        env.ingest(seq=seq)
    assert env.of_type("gap") == []
    env.ingest(seq=6)
    (gap,) = env.of_type("gap")
    assert (gap["first_missing_seq"], gap["last_missing_seq"], gap["missing_count"], gap["gap_id"]) == (3, 5, 3, "gap-0001")
    samples = env.of_type("sample")
    assert gap["start_time"] == samples[2]["recv_time"] and gap["end_time"] == samples[3]["recv_time"] == gap["recv_time"]


def test_duplicate_and_reset_are_anomalies_that_keep_the_nested_sample_and_move_nothing(env: Env) -> None:
    for seq in (0, 1, 4):
        env.ingest(seq=seq)
    dup = env.ingest(seq=1, rss_bytes=7)
    reset = env.ingest(seq=2)
    assert (dup.http_status, dup.body["status"], reset.body["status"]) == (200, "duplicate", "reset")
    first, second = env.of_type("sequence_anomaly")
    assert (first["anomaly"], first["seq"], first["previous_seq"], first["sample"]["rss_bytes"]) == ("duplicate", 1, 4, 7)
    assert (second["anomaly"], second["seq"], second["previous_seq"]) == ("reset", 2, 4)
    assert env.state().highest_seq == 4 and env.state().seen.ranges() == ((0, 1), (4, 4))
    assert env.ingest(seq=5).body["status"] == "stored" and len(env.of_type("gap")) == 1  # no new gap: the baseline stayed at 4
    summary = summarize(read_evidence(env.path("r1")))
    assert (summary.sample_count, summary.duplicate_count, summary.reset_count, summary.missing_sample_count) == (4, 1, 1, 2)


def test_a_duplicate_of_the_highest_number_is_a_duplicate(env: Env) -> None:
    env.ingest(seq=0)
    assert env.ingest(seq=0).body["status"] == "duplicate"
    assert env.of_type("sequence_anomaly")[0]["previous_seq"] == 0


def test_a_late_sample_stays_a_reset_and_its_numbers_stay_missing(env: Env) -> None:
    env.ingest(seq=0)
    env.ingest(seq=3)
    assert env.ingest(seq=1).body["status"] == "reset"
    assert summarize(read_evidence(env.path("r1"))).missing_sample_count == 2


# --- drops and reconciliation ----------------------------------------------------------------------------


def test_a_known_drop_matching_the_discontinuity_is_reconciled_in_one_write(env: Env) -> None:
    env.ingest(seq=0)
    result = env.ingest(seq=4, dropped=3)
    disc, drop = env.of_type("gap")
    assert result.gap_ids == ("gap-0001", "gap-0002") and (disc["gap_id"], drop["gap_id"]) == result.gap_ids
    assert (disc["first_missing_seq"], disc["last_missing_seq"]) == (drop["first_missing_seq"], drop["last_missing_seq"]) == (1, 3)
    assert (disc["reconciliation"], disc["reconciles_with"]) == ("reconciled", ["gap-0002"])
    assert (drop["reconciliation"], drop["reconciles_with"]) == ("reconciled", ["gap-0001"])
    assert (drop["dropped_count"], drop["range_status"], "missing_count" in drop) == (3, "known", False)
    types = [r["record_type"] for r in env.records()]
    assert types == ["run_metadata", "sample", "sample", "gap", "gap"]  # the Sample first, then its gaps
    summary = summarize(read_evidence(env.path("r1")))
    assert (summary.missing_sample_count, summary.unreconciled_drop_count) == (3, 0) and summary.warnings == []


def test_a_partially_overlapping_drop_is_marked_partial_overlap_on_both(env: Env) -> None:
    env.ingest(seq=0)
    env.ingest(seq=5, dropped=2)
    disc, drop = env.of_type("gap")
    assert (disc["first_missing_seq"], disc["last_missing_seq"], drop["first_missing_seq"], drop["last_missing_seq"]) == (1, 4, 3, 4)
    assert (disc["reconciliation"], drop["reconciliation"]) == ("partial_overlap", "partial_overlap")
    assert (disc["reconciles_with"], drop["reconciles_with"]) == ([drop["gap_id"]], [disc["gap_id"]])
    summary = summarize(read_evidence(env.path("r1")))
    assert (summary.missing_sample_count, summary.unreconciled_drop_count, summary.warnings) == (4, 0, [])


def test_a_drop_overlapping_received_samples_is_unknown_and_keeps_its_count(env: Env) -> None:
    for seq in (0, 1, 2):
        env.ingest(seq=seq)
    env.ingest(seq=3, dropped=2)  # candidate [1, 2] was received (an acknowledgement was lost)
    (drop,) = env.of_type("gap")
    assert (drop["range_status"], drop["first_missing_seq"], drop["last_missing_seq"], drop["dropped_count"]) == ("unknown", None, None, 2)
    assert drop["range_note"] == "range overlaps received samples" and (drop["reconciliation"], drop["reconciles_with"]) == ("none", [])
    summary = summarize(read_evidence(env.path("r1")))
    assert (summary.missing_sample_count, summary.unreconciled_drop_count, summary.unreconciled_drop_uncertain) == (0, 2, True)


def test_a_drop_reaching_below_zero_is_unknown_next_to_a_first_sample_discontinuity(env: Env) -> None:
    env.ingest(seq=1, dropped=5)
    disc, drop = env.of_type("gap")
    assert (disc["first_missing_seq"], disc["last_missing_seq"], disc["reconciliation"]) == (0, 0, "none")
    assert (drop["range_status"], drop["range_note"], drop["dropped_count"]) == ("unknown", "range starts below sequence number 0", 5)


def test_a_drop_on_a_duplicate_is_ignored(env: Env) -> None:
    env.ingest(seq=0)
    env.ingest(seq=0, dropped=3)
    assert env.of_type("gap") == [] and env.of_type("sequence_anomaly")[0]["sample"]["dropped"] == 3


def test_gap_ids_are_unique_across_ingests_and_restarts(env: Env) -> None:
    env.ingest(seq=0)
    env.ingest(seq=2)
    env.ingest(seq=4, dropped=1)
    env.restart()
    env.ingest(seq=7)
    env.restart()
    env.ingest(seq=9)
    ids = [g["gap_id"] for g in env.of_type("gap")]
    assert ids == [f"gap-{n:04d}" for n in range(1, len(ids) + 1)] and len(ids) == 5


# --- replay and recovery ---------------------------------------------------------------------------------


def test_replay_regenerates_missing_gap_records_once_in_one_append_and_is_idempotent(env: Env) -> None:
    hand_file(env, "r1", sample_rec("r1", 0), sample_rec("r1", 5, dropped=2), sample_rec("r1", 6))
    env.restart()
    assert env.ingest(seq=7).http_status == 200
    disc, drop = env.of_type("gap")
    assert (disc["gap_id"], drop["gap_id"]) == ("gap-0001", "gap-0002")
    assert (disc["gap_type"], disc["first_missing_seq"], disc["last_missing_seq"], disc["detected_at_seq"]) == ("sequence_discontinuity", 1, 4, 5)
    assert (drop["range_status"], drop["first_missing_seq"], drop["last_missing_seq"], drop["dropped_count"]) == ("known", 3, 4, 2)
    assert (disc["reconciliation"], drop["reconciliation"]) == ("partial_overlap", "partial_overlap")
    assert disc["start_time"] == format_utc_ms(BASE) and disc["end_time"] == drop["end_time"] == format_utc_ms(BASE + timedelta(seconds=5))
    assert disc["recv_time"] == drop["recv_time"] != disc["end_time"]  # recovery time, not back-dated
    report = env.service.replay_reports["r1"]
    assert (report.regenerated_gap_ids, report.conflicts, report.replayed_samples) == (("gap-0001", "gap-0002"), (), 3)
    types = [r["record_type"] for r in env.records()]
    assert types == ["sample", "sample", "sample", "gap", "gap", "run_metadata", "sample"]  # gaps in one append, before new Samples
    before = env.path("r1").read_bytes()
    env.restart()
    env.ingest(seq=8)
    assert env.service.replay_reports["r1"].regenerated_gap_ids == ()
    assert [g["gap_id"] for g in env.of_type("gap")] == ["gap-0001", "gap-0002"]
    assert env.path("r1").read_bytes().startswith(before)  # nothing historical was rewritten


def test_replay_matches_stored_gaps_by_identity_not_by_id_or_time(env: Env) -> None:
    stored = gap_rec("r1", "gap-0042", 1, 2, detected=3)
    stored["start_time"] = stored["end_time"] = "2030-01-01T00:00:00.000Z"
    hand_file(env, "r1", sample_rec("r1", 0), sample_rec("r1", 3), stored)
    env.restart()
    env.ingest(seq=4)
    assert [g["gap_id"] for g in env.of_type("gap")] == ["gap-0042"] and env.service.replay_reports["r1"].regenerated_gap_ids == ()
    env.ingest(seq=7)
    assert [g["gap_id"] for g in env.of_type("gap")] == ["gap-0042", "gap-0043"]  # new ids continue after the highest stored


def test_regenerated_gaps_take_ids_after_the_highest_stored_id_and_reconcile_with_stored_peers(env: Env) -> None:
    stored = gap_rec("r1", "gap-0007", 1, 2, detected=3)
    hand_file(env, "r1", sample_rec("r1", 0), sample_rec("r1", 3), stored, sample_rec("r1", 6, dropped=2))
    env.restart()
    env.ingest(seq=7)
    gaps = env.of_type("gap")
    assert [g["gap_id"] for g in gaps] == ["gap-0007", "gap-0008", "gap-0009"]  # [4, 5] at 6, then its drop [4, 5]
    disc, drop = gaps[1], gaps[2]
    assert (disc["first_missing_seq"], disc["last_missing_seq"], drop["first_missing_seq"], drop["last_missing_seq"]) == (4, 5, 4, 5)
    assert (disc["reconciliation"], disc["reconciles_with"]) == ("reconciled", ["gap-0009"])
    assert (drop["reconciliation"], drop["reconciles_with"]) == ("reconciled", ["gap-0008"])  # the other ingest's gap-0007 is no peer


def test_replay_reports_a_conflict_and_adds_and_overwrites_nothing(env: Env) -> None:
    wrong = gap_rec("r1", "gap-0001", 1, 1, detected=3)  # the Sample at 3 implies [1, 2]
    path = hand_file(env, "r1", sample_rec("r1", 0), sample_rec("r1", 3), wrong)
    original = path.read_bytes()
    env.restart()
    assert env.ingest(seq=4).http_status == 200
    (report,) = [env.service.replay_reports["r1"]]
    assert report.regenerated_gap_ids == () and len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert (conflict.gap_type, conflict.detected_at_seq, conflict.stored[0][0]) == ("sequence_discontinuity", 3, "gap-0001")
    assert len(env.of_type("gap")) == 1 and path.read_bytes().startswith(original)


def test_replay_never_regenerates_silence_gaps_or_uses_anomalies_for_the_baseline(env: Env) -> None:
    silence = {
        "record_type": "gap", "schema_version": 1, "run_id": "r1", "recv_time": format_utc_ms(BASE), "gap_id": "gap-0001",
        "gap_type": "silence_timeout", "start_time": format_utc_ms(BASE), "end_time": format_utc_ms(BASE),
        "last_sample_time": format_utc_ms(BASE), "detected_time": format_utc_ms(BASE), "threshold_ms": 1500, "interval_ms": 500,
        "sample_count": "unknown", "reconciliation": "not_applicable", "reconciles_with": [],
    }
    anomaly = {
        "record_type": "sequence_anomaly", "schema_version": 1, "run_id": "r1", "recv_time": format_utc_ms(BASE), "anomaly": "duplicate",
        "seq": 5, "previous_seq": 5, "sample": sample(seq=5),  # a replay that counted it as a Sample would see a gap [2, 4]
    }
    hand_file(env, "r1", sample_rec("r1", 0), sample_rec("r1", 1), silence, anomaly, sample_rec("r1", 2))
    env.restart()
    env.ingest(seq=3)  # consecutive: the anomaly did not move the baseline
    assert [g["gap_type"] for g in env.of_type("gap")] == ["silence_timeout"]
    assert env.service.replay_reports["r1"].regenerated_gap_ids == ()


def test_replay_excludes_malformed_and_isolated_lines(env: Env) -> None:
    first = serialize_record(sample_rec("r1", 3))
    path = hand_file(env, "r1", sample_rec("r1", 0), sample_rec("r1", 2), raw_tail=b"not json\n" + first[: len(first) // 2])
    env.restart()
    result = env.ingest(seq=3)  # the cut Sample 3 was never acknowledged and is not in the file as a valid record
    assert result.body["status"] == "stored"
    gaps = env.of_type("gap")
    assert [(g["first_missing_seq"], g["last_missing_seq"], g["detected_at_seq"]) for g in gaps] == [(1, 1, 2)]
    assert [r["sample"]["seq"] for r in env.of_type("sample")] == [0, 2, 3]
    assert len(env.of_type("repair")) == 1 and len(read_evidence(path).errors) >= 2


def test_an_acknowledgement_lost_retry_is_a_duplicate_before_and_after_a_restart(env: Env) -> None:
    env.ingest(seq=0)
    env.ingest(seq=1)  # imagine the response was lost
    assert env.ingest(seq=1).body["status"] == "duplicate"
    env.restart()
    assert env.ingest(seq=1).body["status"] == "duplicate"
    assert len(env.of_type("sample")) == 2 and len(env.of_type("sequence_anomaly")) == 2 and env.of_type("gap") == []


# --- storage failure -------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["write", "partial", "flush", "fsync"])
def test_a_failed_append_is_503_without_acknowledgement_and_the_next_ingest_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    env = Env(tmp_path)
    faults = Faults(monkeypatch)
    env.ingest(seq=0)
    before = (env.state().highest_seq, env.state().seen.ranges(), env.state().next_gap_number)
    invalidations = env.probe.invalidations
    if mode == "fsync":
        env.rec.fail_file = True
    else:
        faults.arm(mode, "r1.jsonl")
    result = env.ingest(seq=3)  # sample plus a discontinuity gap
    assert (result.http_status, result.body, result.kind) == (503, {"error": "storage_failure"}, "storage_failure")
    assert result.durability is None and env.probe.invalidations == invalidations + 1
    state = env.state()
    assert state.dirty and (state.highest_seq, state.seen.ranges(), state.next_gap_number) == before
    env.rec.fail_file = False
    faults.disarm()
    retry = env.ingest(seq=3)
    assert retry.http_status == 200 and retry.body["status"] in ("stored", "duplicate") and not env.state().dirty
    assert env.ingest(seq=4).body["status"] == "stored"
    records = env.records()
    assert [r["sample"]["seq"] for r in records if r["record_type"] == "sample"].count(3) == 1
    gaps = [g for g in records if g["record_type"] == "gap"]
    assert [(g["first_missing_seq"], g["last_missing_seq"]) for g in gaps] == [(1, 2)]  # exactly one gap, never two
    summary = summarize(read_evidence(env.path("r1")))
    assert (summary.sample_count, summary.missing_sample_count) == (3, 2)
    assert all(validate_record(r) == [] for r in records)


def test_a_partial_batch_with_the_sample_but_not_its_gap_is_repaired_and_the_gap_regenerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    faults = Faults(monkeypatch)
    env.ingest(seq=0)
    sample_line_len = len(serialize_record(env.of_type("sample")[0]))
    faults.arm("partial", "r1.jsonl", limit=sample_line_len + 20)  # the Sample line is complete, the gap line is cut
    assert env.ingest(seq=3).http_status == 503
    faults.disarm()
    assert env.path("r1").read_bytes()[-1:] != b"\n"
    assert env.ingest(seq=3).body["status"] == "duplicate"  # the persisted Sample makes the retry an ack-lost duplicate
    (gap,) = env.of_type("gap")
    assert (gap["gap_id"], gap["first_missing_seq"], gap["last_missing_seq"]) == ("gap-0001", 1, 2)
    assert len(env.of_type("repair")) == 1 and env.service.replay_reports["r1"].regenerated_gap_ids == ("gap-0001",)


def test_a_failed_run_metadata_write_is_503_and_invalidates_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path)
    Faults(monkeypatch).arm("write", "r1.jsonl")
    result = env.ingest(seq=0)
    assert result.http_status == 503 and env.probe.invalidations == 1 and env.state().dirty and not env.state().loaded
    assert env.of_type("sample") == []
    assert env.ingest(seq=0).body["status"] == "stored"


@pytest.mark.parametrize("target", ["rejections", "run"])
def test_a_failed_rejection_write_keeps_422_and_reports_rejection_recorded_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    env = Env(tmp_path)
    faults = Faults(monkeypatch)
    if target == "run":
        env.ingest(seq=0)
        faults.arm("partial", "r1.jsonl")
        result = env.ingest(seq=1, ts="bad")
    else:
        faults.arm("partial", "_rejections.jsonl")
        result = env.service.ingest(b"{")
    assert result.http_status == 422 and result.body["rejection_recorded"] is False and result.rejection_recorded is False
    assert env.probe.invalidations == 1 and result.kind == "rejected"
    faults.disarm()
    again = env.ingest(seq=1, ts="bad") if target == "run" else env.service.ingest(b"{")
    assert again.http_status == 422 and "rejection_recorded" not in again.body and again.rejection_recorded is True
    if target == "run":
        assert env.state().dirty is False and len(env.of_type("rejection")) == 1
    else:
        assert [r["record_type"] for r in env.rejections()] == ["repair", "rejection"]  # the partial line was repaired first


# --- termination -----------------------------------------------------------------------------------------


def test_termination_is_recorded_before_the_run_is_marked_terminated(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.ingest(seq=0)
    registry = env.service.registry
    original = registry.append
    flags: list[bool] = []

    def spy(state: Any, records: Any) -> Any:
        flags.append(state.terminated)
        out = original(state, records)
        flags.append(state.terminated)
        return out

    monkeypatch.setattr(registry, "append", spy)
    result = env.service.record_termination("r1", body(TERMINATION))
    assert (result.http_status, result.body, result.recorded) == (200, {"status": "recorded"}, True)
    assert flags == [False, False] and env.state().terminated is True
    (record,) = env.of_type("termination")
    assert validate_record(record) == [] and record["cause_label"] == "docker_oom_kill" and record["container_state"]["exit_code"] == 137
    assert env.ingest(seq=1).body["status"] == "stored"  # late pre-kill Samples are still accepted
    assert summarize(read_evidence(env.path("r1"))).termination_cause == "docker_oom_kill"


@pytest.mark.parametrize(
    ("change", "code", "field"),
    [
        ({"cause_label": "oom"}, "out_of_range", "cause_label"),
        ({"source": "somebody"}, "out_of_range", "source"),
        ({"container_state": {"status": "exited", "exit_code": "137", "oom_killed": True}}, "wrong_type", "container_state.exit_code"),
        ({"observed_time": "now"}, "invalid_format", "observed_time"),
        ({"extra": 1}, "unknown_field", "extra"),
        ({"run_id": "other"}, "unknown_field", "run_id"),
        ({"recv_time": "2025-01-01T00:00:00.000Z"}, "unknown_field", "recv_time"),
    ],
)
def test_invalid_termination_data_is_rejected_and_nothing_is_marked(env: Env, change: dict, code: str, field: str) -> None:
    env.ingest(seq=0)
    result = env.service.record_termination("r1", body({**TERMINATION, **change}))
    assert (result.http_status, result.recorded, result.body["reason_code"], result.body["field"]) == (422, False, code, field)
    assert env.state().terminated is False and env.of_type("termination") == []
    assert env.of_type("rejection")[-1]["reason_code"] == code


def test_a_missing_termination_field_and_bad_json_and_bad_run_id_are_rejected(env: Env) -> None:
    incomplete = {k: v for k, v in TERMINATION.items() if k != "note"}
    assert env.service.record_termination("r1", body(incomplete)).body["reason_code"] == "missing_field"
    assert env.service.record_termination("r1", b"{").body["reason_code"] == "invalid_json"
    assert env.service.record_termination("r1", b"[]").body["reason_code"] == "wrong_type"
    bad = env.service.record_termination("bad id", body(TERMINATION))
    assert (bad.http_status, bad.body["reason_code"]) == (422, "invalid_run_id")
    assert [r["run_id"] for r in env.rejections()] == [None] and len(env.of_type("rejection")) == 3


def test_a_failed_termination_write_is_503_not_terminated_and_invalidates_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    faults = Faults(monkeypatch)
    env.ingest(seq=0)
    faults.arm("write", "r1.jsonl")
    result = env.service.record_termination("r1", body(TERMINATION))
    assert (result.http_status, result.body, result.recorded) == (503, {"error": "storage_failure"}, False)
    assert env.state().terminated is False and env.state().dirty and env.probe.invalidations == 1
    faults.disarm()
    assert env.service.record_termination("r1", body(TERMINATION)).recorded is True and env.state().terminated is True
    assert len(env.of_type("termination")) == 1


# --- duplicate JSON keys (D33) ---------------------------------------------------------------------------

_DUPLICATE_BODIES = {
    "top level": b'{"run_id":"r1","run_id":"r2","seq":0}',
    "nested null_reasons": b'{"run_id":"r1","null_reasons":{"rss_bytes":"a","rss_bytes":"b"},"seq":0}',
    "nested deeper": b'{"run_id":"r1","x":[{"y":{"k":1,"k":2}}]}',
    "escape equivalent": b'{"a":1,"\\u0061":2}',
    "equal values": b'{"seq":0,"seq":0}',
}


def test_the_parser_rejects_duplicate_keys_at_any_depth_and_after_escape_decoding() -> None:
    from coroner.collector.service import _DuplicateKeyError, _parse_body

    for name, raw in _DUPLICATE_BODIES.items():
        with pytest.raises(_DuplicateKeyError):
            _parse_body(raw)
        assert name


def test_the_parser_accepts_the_same_key_in_separate_objects_and_keeps_json_semantics() -> None:
    from coroner.collector.service import _parse_body

    value = _parse_body(b'{"b":1,"a":[{"k":1},{"k":2}],"c":{"k":3},"d":1.0,"e":-0.0,"f":10000000000000000000000,"g":null,"b2":true}')
    assert type(value) is dict and list(value) == ["b", "a", "c", "d", "e", "f", "g", "b2"]
    assert value["a"] == [{"k": 1}, {"k": 2}] and value["c"] == {"k": 3}
    assert type(value["d"]) is float and type(value["f"]) is int and value["f"] == 10**22
    assert str(value["e"]) == "-0.0" and value["b2"] is True
    assert _parse_body(b'{"k":1,"j":{"k":2}}') == {"k": 1, "j": {"k": 2}}  # same key at different levels is fine
    assert _parse_body(b'{"a":1,"\\u0062":2}') == {"a": 1, "b": 2}


@pytest.mark.parametrize("name", list(_DUPLICATE_BODIES))
def test_ingest_duplicate_keys_are_422_invalid_json_in_rejections_with_a_null_run_id(env: Env, name: str) -> None:
    raw = _DUPLICATE_BODIES[name]
    result = env.service.ingest(raw)
    assert (result.http_status, result.kind, result.body["reason_code"], result.run_id) == (422, "rejected", "invalid_json", None)
    (rejection,) = env.rejections()
    assert rejection["run_id"] is None and rejection["reason_code"] == "invalid_json" and rejection["http_status"] == 422
    assert base64.b64decode(rejection["body_preview_b64"]) == raw[:1024] and rejection["body_len"] == len(raw)
    assert not env.path("r1").exists()  # the run named inside the body is not trusted


def test_a_full_sample_with_a_repeated_field_is_not_stored_with_the_last_value(env: Env) -> None:
    text = json.dumps(sample(seq=0))
    raw = (text[:-1] + ',"seq":5}').encode()
    result = env.service.ingest(raw)
    assert (result.http_status, result.body["reason_code"]) == (422, "invalid_json")
    assert not env.path("r1").exists() and env.service.registry.get("r1").highest_seq is None


def test_termination_duplicate_keys_are_422_invalid_json_in_the_url_run_file(env: Env) -> None:
    env.ingest(seq=0)
    text = json.dumps(TERMINATION)
    cases = [
        text[:-1] + ',"note":"again"}',
        text.replace('"exit_code": 137', '"exit_code": 137, "exit_code": 0'),
        text.replace('"source"', '"\\u0073ource": "x", "source"', 1),
    ]
    for case in cases:
        raw = case.encode()
        result = env.service.record_termination("r1", raw)
        assert (result.http_status, result.recorded, result.body["reason_code"], result.run_id) == (422, False, "invalid_json", "r1")
        last = env.of_type("rejection")[-1]
        assert last["run_id"] == "r1" and last["reason_code"] == "invalid_json" and base64.b64decode(last["body_preview_b64"]) == raw[:1024]
    assert len(env.of_type("rejection")) == 3 and env.of_type("termination") == [] and env.state().terminated is False
    assert not (env.directory / "_rejections.jsonl").exists()


def test_a_plain_unparseable_termination_body_is_recorded_in_the_url_run_file(env: Env) -> None:
    env.ingest(seq=0)
    result = env.service.record_termination("r1", b"{")
    assert (result.http_status, result.body["reason_code"]) == (422, "invalid_json")
    assert [r["reason_code"] for r in env.of_type("rejection")] == ["invalid_json"] and env.of_type("rejection")[0]["run_id"] == "r1"
    assert not (env.directory / "_rejections.jsonl").exists()


def test_duplicate_keys_never_escape_as_an_exception_or_a_500(env: Env) -> None:
    for raw in _DUPLICATE_BODIES.values():
        assert env.service.ingest(raw).http_status == 422
        assert env.service.record_termination("r1", raw).http_status == 422


# --- construction and pure helpers -----------------------------------------------------------------------


def test_the_service_requires_the_replay_hook(tmp_path: Path) -> None:
    config = CollectorConfig(Durability.PROCESS, 65536, 50, tmp_path / "evidence")
    with pytest.raises(ValueError, match="GapReplay"):
        IngestService(RunRegistry(config))
    with pytest.raises(RuntimeError, match="not attached"):
        GapReplay()("r1", tmp_path / "r1.jsonl", read_evidence(b""))


def test_build_records_for_an_anomaly_has_no_gap_and_keeps_the_sample() -> None:
    state = SeqState(5, IntervalSet([(0, 5)]))
    obj = sample(seq=2)
    records, ids = build_records("r1", obj, "2025-01-01T00:00:00.000Z", classify(state, 2, 0), "x", 9)
    assert ids == () and [r["record_type"] for r in records] == ["sequence_anomaly"]
    assert records[0]["sample"] == obj and records[0]["previous_seq"] == 5 and validate_record(records[0]) == []


# --- properties ------------------------------------------------------------------------------------------


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(hs.lists(hs.tuples(hs.integers(0, 12), hs.integers(0, 4)), min_size=1, max_size=14))
def test_property_live_gaps_match_the_model_and_replay_regenerates_exactly_the_same_gaps(stream: list[tuple[int, int]]) -> None:
    """**Validates: Requirements 4.2, 4.3, 6.1, 6.3, 6.4** (live and replayed gap records agree and cover every missing number once)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        model = SeqState()
        accepted: set[int] = set()
        for seq, dropped in stream:
            events = classify(model, seq, dropped)
            result = env.ingest(seq=seq, dropped=dropped)
            assert result.http_status == 200 and result.body["status"] == ("stored" if events.kind == "new" else events.kind)
            if events.drop is not None:
                assert events.drop.range == derive_drop_range(seq, dropped, model.seen)
            model.apply(events)
            if events.kind == "new":
                accepted.add(seq)
        records = env.records()
        assert all(validate_record(r) == [] for r in records) and read_evidence(env.path("r1")).errors == []
        live_gaps = [g for g in records if g["record_type"] == "gap"]
        ids = [g["gap_id"] for g in live_gaps]
        assert len(ids) == len(set(ids))
        summary = summarize(read_evidence(env.path("r1")))
        highest = max(accepted)
        assert summary.missing_sample_count == highest + 1 - len(accepted) and summary.warnings == []
        # replay: strip every gap record, restart, open the run; the regenerated gaps equal the live ones except recv_time
        kept = [line for line in env.path("r1").read_bytes().splitlines(keepends=True) if json.loads(line)["record_type"] != "gap"]
        env.path("r1").write_bytes(b"".join(kept))
        env.restart()
        with env.service.registry.lock("r1"):
            env.service.registry.open_run("r1")
        replayed = [g for g in env.records() if g["record_type"] == "gap"]
        strip = lambda g: {k: v for k, v in g.items() if k != "recv_time"}  # noqa: E731
        assert [strip(g) for g in replayed] == [strip(g) for g in live_gaps]
        env.restart()
        with env.service.registry.lock("r1"):
            env.service.registry.open_run("r1")
        assert [g for g in env.records() if g["record_type"] == "gap"] == replayed  # idempotent


def test_default_clock_wiring_produces_valid_timestamps(tmp_path: Path) -> None:
    config = CollectorConfig(Durability.PROCESS, 65536, 50, tmp_path / "evidence")
    service = create_ingest_service(config)
    service.registry.start()
    try:
        result = service.ingest(body(sample()))
        assert result.http_status == 200 and result.durability == "process" and os.path.exists(config.evidence_dir / "r1.jsonl")
        assert validate_record(read_evidence(config.evidence_dir / "r1.jsonl").records[1]) == []
    finally:
        service.registry.close()


def test_late_samples_after_termination_never_clear_terminated_or_reopen_silence(env: Env) -> None:
    env.ingest(seq=0)
    assert env.service.record_termination("r1", body(TERMINATION)).recorded is True
    for seq in (1, 3, 3, 2):  # new, new with a gap, duplicate, reset
        assert env.ingest(seq=seq).http_status == 200
        state = env.state()
        assert state.terminated is True and state.silence_open is False
    assert env.state().last_sample_monotonic is not None  # a late Sample is still a received Sample
    env.restart()
    assert env.ingest(seq=4).http_status == 200
    assert env.state().terminated is True
    assert len(env.of_type("termination")) == 1


# --- recv_time is the Receive_Time, read when the request starts (Task 3.7 correction 2) -----------------


class GatedClock(Clock):
    """Clock whose first reading on thread `gated` takes its value and then blocks until `release` is set.

    The gate sits right after the request's arrival reading, so the request is held before the size check,
    validation and the run lock.
    """

    def __init__(self) -> None:
        super().__init__()
        self.mutex = threading.Lock()
        self.gated: threading.Thread | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self._done = False
        self.first: dict[str, str] = {}  # thread name -> its first reading, i.e. the arrival time of its request

    def now(self) -> str:
        with self.mutex:
            value = super().now()
            self.first.setdefault(threading.current_thread().name, value)
        if threading.current_thread() is self.gated and not self._done:
            self._done = True
            self.entered.set()
            assert self.release.wait(10), "the gated reader was never released"
        return value


BAD_TS = ("bad-ts", lambda s: s.ingest(body(sample("r1", 5, ts="bad"))), "r1")
GOOD_0 = ("good", lambda s: s.ingest(body(sample("r1", 0))), "r1")
GOOD_1 = ("good", lambda s: s.ingest(body(sample("r1", 1))), "r1")
TERMINATE = ("termination", lambda s: s.record_termination("r1", body(TERMINATION)), "r1")
BAD_JSON = ("bad-json", lambda s: s.ingest(b"{"), "_rejections")
OVERSIZED = ("413", lambda s: s.ingest(b"x" * 70000), "_rejections")


@pytest.mark.parametrize(
    "first, second",
    [
        pytest.param(GOOD_0, GOOD_1, id="sample-sample"),
        pytest.param(BAD_TS, GOOD_0, id="run-rejection-sample"),
        pytest.param(TERMINATE, GOOD_0, id="termination-sample"),
        pytest.param(GOOD_0, TERMINATE, id="sample-termination"),
        pytest.param(BAD_JSON, OVERSIZED, id="rejections-file-json-413"),
        pytest.param(OVERSIZED, BAD_JSON, id="rejections-file-413-json"),
    ],
)
def test_recv_time_is_the_arrival_time_even_when_the_earlier_request_persists_later(
    tmp_path: Path, first: tuple[Any, ...], second: tuple[Any, ...]
) -> None:
    """Request A reads the clock first (earlier time) and is then held before it takes the run lock.

    B arrives after A, gets a later time, and runs to completion. A is released and persists afterwards. Both records
    keep the time read when their request started, so A's earlier time appears after B's later time in the file.
    Re-reading the clock under the lock would stamp A with a time after B's, and this test would fail.
    """
    env = Env(tmp_path)
    clock = GatedClock()
    env.clock = clock
    env.restart()
    results: dict[str, Any] = {}

    def run(label: str, call: Any) -> None:
        results[label] = call(env.service)

    a = threading.Thread(target=run, args=("a", first[1]), name="req-a")
    b = threading.Thread(target=run, args=("b", second[1]), name="req-b")
    clock.gated = a
    a.start()
    assert clock.entered.wait(10)
    b.start()
    b.join(10)  # B does not wait for A: A holds no lock yet
    assert not b.is_alive(), "B should complete while A is held back before the run lock"
    clock.release.set()
    a.join(10)
    assert not a.is_alive()

    time_a, time_b = clock.first["req-a"], clock.first["req-b"]
    assert time_a < time_b
    by_file: dict[str, list[dict[str, Any]]] = {}
    for name in {first[2], second[2]}:
        by_file[name] = [r for r in read_evidence(env.directory / f"{name}.jsonl").records if r["record_type"] != "run_metadata"]
    for label, name, value in (("a", first[2], time_a), ("b", second[2], time_b)):
        carriers = [r for r in by_file[name] if r["recv_time"] == value and r["record_type"] != "gap"]
        assert len(carriers) == 1, (label, value, by_file[name])
        reported = getattr(results[label], "recv_time", None)
        if reported is not None:
            assert reported == value, (label, reported, value)
    if first[2] == second[2]:  # one file: A (earlier time) was appended after B (later time)
        times = [r["recv_time"] for r in by_file[first[2]] if r["recv_time"] in (time_a, time_b)]
        assert times.index(time_b) < times.index(time_a), times
        assert times != sorted(times)
    else:  # separate files: each holds its own request's arrival time, and the persist order is B then A
        assert [r["recv_time"] for r in by_file[first[2]] if r["recv_time"] == time_a] == [time_a]


def test_sequence_state_follows_the_append_order_not_the_arrival_order(tmp_path: Path) -> None:
    """A (seq 0) arrives first but persists after B (seq 1): B is `stored` with a gap, A is then a `reset`."""
    env = Env(tmp_path)
    clock = GatedClock()
    env.clock = clock
    env.restart()
    results: dict[str, Any] = {}
    a = threading.Thread(target=lambda: results.update(a=env.service.ingest(body(sample("r1", 0)))), name="req-a")
    b = threading.Thread(target=lambda: results.update(b=env.service.ingest(body(sample("r1", 1)))), name="req-b")
    clock.gated = a
    a.start()
    assert clock.entered.wait(10)
    b.start()
    b.join(10)
    assert not b.is_alive()
    clock.release.set()
    a.join(10)
    assert not a.is_alive()

    assert results["b"].body["status"] == "stored" and results["a"].body["status"] == "reset"
    assert results["a"].recv_time == clock.first["req-a"] and results["b"].recv_time == clock.first["req-b"]
    kinds = [(r["record_type"], r["recv_time"]) for r in env.records("r1") if r["record_type"] != "run_metadata"]
    assert kinds == [
        ("sample", clock.first["req-b"]), ("gap", clock.first["req-b"]), ("sequence_anomaly", clock.first["req-a"]),
    ]
    state = env.state()
    assert state.highest_seq == 1 and state.next_gap_number == 2
