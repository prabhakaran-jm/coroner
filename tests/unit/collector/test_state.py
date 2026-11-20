"""Task 3.2: RunState, RunRegistry, startup recovery and the volume lock (design D26; Requirements 4.16, 5.2, 5.3, 5.5).

Everything runs in pytest temporary directories with a recording double for `fsync` and the directory fsync, so no
host crash, Docker or network is needed. Tests that need a real POSIX `flock` are skipped on win32.
"""
from __future__ import annotations

import builtins
import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as hs

from coroner.collector import state as state_module
from coroner.collector.config import CollectorConfig
from coroner.collector.state import (
    COLLECTOR_VERSION,
    LOCK_FILE_NAME,
    CollectorAlreadyRunning,
    CollectorStartupError,
    NewSample,
    Outcome,
    RunDirtyError,
    RunRegistry,
    RunState,
    VolumeLock,
    gap_id_number,
    scan_records,
)
from coroner.evidence import writer as writer_module
from coroner.evidence.reader import read_evidence
from coroner.evidence.schema import validate_record
from coroner.evidence.writer import Durability, serialize_record

RECV = "2025-01-01T12:00:06.530Z"
NOW = "2025-01-01T12:00:07.000Z"


# --- builders --------------------------------------------------------------------------------------------


def envelope(record_type: str, run_id: str | None, recv: str = RECV, **extra: object) -> dict:
    return {"record_type": record_type, "schema_version": 1, "run_id": run_id, "recv_time": recv, **extra}


def sample_record(run_id: str, seq: int, interval_ms: int = 500, recv: str = RECV) -> dict:
    sample = {
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 1000, "request_count": 1,
        "loop_delay_ms": 0.5, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": interval_ms, "dropped": 0,
    }
    return envelope("sample", run_id, recv, sample=sample)


def anomaly_record(run_id: str, seq: int, previous: int) -> dict:
    inner = sample_record(run_id, seq)["sample"]
    return envelope("sequence_anomaly", run_id, anomaly="duplicate", seq=seq, previous_seq=previous, sample=inner)


def discontinuity_gap(run_id: str, gap_id: str, first: int, last: int) -> dict:
    return envelope(
        "gap", run_id, gap_id=gap_id, gap_type="sequence_discontinuity", start_time=RECV, end_time=RECV,
        first_missing_seq=first, last_missing_seq=last, missing_count=last - first + 1, reconciliation="none",
        reconciles_with=[], detected_at_seq=last + 1,
    )


def silence_gap(run_id: str, gap_id: str = "gap-0001") -> dict:
    return envelope(
        "gap", run_id, gap_id=gap_id, gap_type="silence_timeout", start_time=RECV, end_time=RECV, last_sample_time=RECV,
        detected_time=RECV, threshold_ms=1500, interval_ms=500, sample_count="unknown", reconciliation="not_applicable",
        reconciles_with=[],
    )


def termination_record(run_id: str) -> dict:
    return envelope(
        "termination", run_id, source="experiment_runner", observed_time=RECV,
        container_state={"status": "exited", "exit_code": 137, "oom_killed": True}, cause_label="unknown", note="x",
    )


def lines(*records: dict) -> bytes:
    for record in records:
        assert validate_record(record) == [], record  # fixtures must be valid records
    return b"".join(serialize_record(r) for r in records)


class Rec:
    """Recording double for fsync and directory fsync. Each fsync logs the file size on disk at that moment."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.fail_file = False

    def fsync(self, fd: int) -> None:
        if self.fail_file:
            raise OSError("fsync failed")
        self.events.append(("fsync_file", os.fstat(fd).st_size))

    def fsync_dir(self, path: Path) -> None:
        self.events.append(("fsync_dir", Path(path)))


def config_for(directory: Path, level: Durability) -> CollectorConfig:
    return CollectorConfig(durability=level, max_body_bytes=65536, watchdog_tick_ms=50, evidence_dir=directory)


def make(tmp_path: Path, level: Durability = Durability.CRASH, **kwargs) -> tuple[RunRegistry, Rec, Path]:
    directory = tmp_path / "evidence"
    rec = kwargs.pop("rec", None) or Rec()
    registry = RunRegistry(config_for(directory, level), fsync=rec.fsync, fsync_dir=rec.fsync_dir, now=lambda: NOW, **kwargs)
    return registry, rec, directory


def started(tmp_path: Path, level: Durability = Durability.CRASH, **kwargs) -> tuple[RunRegistry, Rec, Path]:
    registry, rec, directory = make(tmp_path, level, **kwargs)
    registry.start()
    rec.events.clear()
    return registry, rec, directory


def open_state(registry: RunRegistry, run_id: str) -> RunState:
    with registry.lock(run_id):
        return registry.open_run(run_id)


def seed(tmp_path: Path, name: str, data: bytes) -> Path:
    directory = tmp_path / "evidence"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


# --- gap identifiers and pure state scan -----------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [("gap-0007", 7), ("gap-0", 0), ("gap-12345678901234567890", 12345678901234567890)])
def test_gap_id_number_accepts_ascii_digits(value: str, expected: int) -> None:
    assert gap_id_number(value) == expected


@pytest.mark.parametrize("value", ["gap-", "gap-x", "gap--1", "gap-1.5", "Gap-1", "gap-0007\n", " gap-1", "gap-１２", "gap-٣", "gap-1 ", "", None, 7, b"gap-1"])
def test_gap_id_number_rejects_everything_else(value: object) -> None:
    assert gap_id_number(value) is None


def test_scan_of_no_records_is_empty() -> None:
    snapshot = scan_records([])
    assert (snapshot.highest_seq, snapshot.seen.ranges(), snapshot.next_gap_number) == (None, (), 1)
    assert not (snapshot.silence_open or snapshot.terminated or snapshot.has_records)
    assert snapshot.interval_ms is None and snapshot.last_sample_recv_time is None


def test_scan_reconstructs_state_from_valid_records() -> None:
    rid = "r1"
    records = [
        sample_record(rid, 0, 500, "2025-01-01T12:00:01.000Z"), sample_record(rid, 1), sample_record(rid, 2),
        sample_record(rid, 5, 250, "2025-01-01T12:00:09.000Z"),
        anomaly_record(rid, 1, 5),  # an anomaly is not an accepted Sample
        discontinuity_gap(rid, "gap-0007", 3, 4), termination_record(rid),
    ]
    snapshot = scan_records(records)
    assert snapshot.highest_seq == 5 and snapshot.seen.ranges() == ((0, 2), (5, 5))
    assert snapshot.interval_ms == 250 and snapshot.last_sample_recv_time == "2025-01-01T12:00:09.000Z"
    assert snapshot.next_gap_number == 8 and snapshot.terminated and not snapshot.silence_open


def test_anomaly_alone_does_not_count_as_a_sample() -> None:
    snapshot = scan_records([anomaly_record("r1", 1, 5)])
    assert snapshot.highest_seq is None and not snapshot.seen and snapshot.interval_ms is None


def test_silence_open_until_a_later_sample() -> None:
    assert scan_records([sample_record("r1", 0), silence_gap("r1")]).silence_open
    assert not scan_records([sample_record("r1", 0), silence_gap("r1"), sample_record("r1", 1)]).silence_open
    assert scan_records([silence_gap("r1"), sample_record("r1", 0), silence_gap("r1", "gap-0002")]).silence_open


def test_invalid_gap_ids_are_ignored_and_ids_are_never_reused() -> None:
    records = [discontinuity_gap("r1", g, 0, 0) for g in ("gap-0003", "gap-x", "GAP-0099", "gap-", "gap-٣٣", "gap-0002")]
    assert scan_records(records).next_gap_number == 4


@given(
    hs.lists(
        hs.one_of(
            hs.tuples(hs.just("sample"), hs.integers(0, 50)),
            hs.tuples(hs.just("gap"), hs.sampled_from(["gap-0001", "gap-0010", "gap-7", "gap-", "gap-x", "GAP-9", "gap-٣", "gap-0003 ", "gap-00"])),
            hs.tuples(hs.just("silence"), hs.just(0)),
            hs.tuples(hs.just("anomaly"), hs.integers(0, 50)),
            hs.tuples(hs.just("term"), hs.just(0)),
        ),
        max_size=30,
    )
)
def test_scan_matches_a_simple_model(items: list[tuple[str, object]]) -> None:
    """**Validates: Requirements 4.16, 5.2** (state is rebuilt from valid records only)."""
    records: list[dict] = []
    seqs: list[int] = []
    gap_numbers: list[int] = []
    silence = False
    for kind, value in items:
        if kind == "sample":
            records.append(sample_record("r1", value))
            seqs.append(value)
            silence = False
        elif kind == "gap":
            records.append(discontinuity_gap("r1", value, 0, 0))
            tail = value[4:]
            if value.startswith("gap-") and tail.isascii() and tail.isdigit():
                gap_numbers.append(int(tail))
        elif kind == "silence":
            records.append(silence_gap("r1", "gap-0001"))
            gap_numbers.append(1)
            silence = True
        elif kind == "anomaly":
            records.append(anomaly_record("r1", value, value))
        else:
            records.append(termination_record("r1"))
    snapshot = scan_records(records)
    assert snapshot.highest_seq == (max(seqs) if seqs else None)
    assert sorted(set(seqs)) == [n for lo, hi in snapshot.seen.ranges() for n in range(lo, hi + 1)]
    assert snapshot.next_gap_number == (max(gap_numbers) + 1 if gap_numbers else 1)
    assert snapshot.silence_open == silence
    assert snapshot.terminated == any(kind == "term" for kind, _ in items)


# --- RunState.apply --------------------------------------------------------------------------------------


def loaded_state() -> RunState:
    st = RunState("r1", loaded=True)
    st.lock.acquire()
    return st


def test_apply_advances_state_only_for_what_the_outcome_says() -> None:
    st = loaded_state()
    st.silence_open = True
    st.apply(Outcome(new_sample=NewSample(4, RECV, 500, 12.5), gap_ids=("gap-0002", "gap-0005")))
    assert (st.highest_seq, st.seen.ranges(), st.last_sample_recv_time, st.last_sample_monotonic, st.interval_ms) == (4, ((4, 4),), RECV, 12.5, 500)
    assert st.next_gap_number == 6 and not st.silence_open and not st.terminated
    st.apply(Outcome(new_sample=NewSample(2, RECV, 100, 13.0)))  # an older number keeps the baseline
    assert st.highest_seq == 4 and st.seen.ranges() == ((2, 2), (4, 4)) and st.next_gap_number == 6
    st.apply(Outcome(silence_opened=True))
    assert st.silence_open
    st.apply(Outcome(terminated=True))
    assert st.terminated


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome(new_sample=NewSample(-1, RECV, 500, 1.0)),
        Outcome(new_sample=NewSample(True, RECV, 500, 1.0)),
        Outcome(new_sample=NewSample(1, RECV, 500, 1.0), gap_ids=("gap-0001", "bad")),
        Outcome(new_sample=NewSample(1, RECV, 500, 1.0), silence_opened=True),
        Outcome(gap_ids=("gap-9", "gap-x")),
    ],
)
def test_invalid_outcome_changes_nothing(outcome: Outcome) -> None:
    st = loaded_state()
    with pytest.raises(ValueError):
        st.apply(outcome)
    assert (st.highest_seq, st.seen.ranges(), st.next_gap_number, st.last_sample_monotonic) == (None, (), 1, None)


def test_apply_refuses_a_dirty_or_unloaded_run_and_an_unlocked_run() -> None:
    st = loaded_state()
    st.mark_dirty()
    with pytest.raises(RunDirtyError):
        st.apply(Outcome(terminated=True))
    assert not st.terminated
    unloaded = RunState("r2")
    unloaded.lock.acquire()
    with pytest.raises(RunDirtyError):
        unloaded.apply(Outcome(terminated=True))
    with pytest.raises(RuntimeError, match="lock"):
        RunState("r3", loaded=True).apply(Outcome(terminated=True))


# --- startup ---------------------------------------------------------------------------------------------


def test_start_creates_a_missing_directory_and_fsyncs_it_under_crash(tmp_path: Path) -> None:
    registry, rec, directory = make(tmp_path)
    report = registry.start()
    assert directory.is_dir() and report.created_directory and report.evidence_files == ()
    assert rec.events == [("fsync_dir", directory)] and report.synced_directory
    registry.close()


def test_start_fsyncs_the_directory_again_after_every_restart(tmp_path: Path) -> None:
    for attempt in range(2):
        registry, rec, directory = make(tmp_path)
        report = registry.start()
        assert rec.events == [("fsync_dir", directory)] and report.created_directory == (attempt == 0)
        registry.close()


def test_process_durable_start_issues_no_fsync(tmp_path: Path) -> None:
    seed(tmp_path, "r1.jsonl", lines(sample_record("r1", 0)))
    registry, rec, _ = make(tmp_path, Durability.PROCESS)
    report = registry.start()
    assert rec.events == [] and report.synced_files == () and not report.synced_directory
    registry.close()


def test_start_rejects_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    (tmp_path / "evidence").write_bytes(b"x")
    registry, _, _ = make(tmp_path)
    with pytest.raises(CollectorStartupError, match="not a directory"):
        registry.start()


def test_start_requires_a_writable_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(state_module.tempfile, "TemporaryFile", refuse)
    registry, _, _ = make(tmp_path)
    with pytest.raises(CollectorStartupError, match="not writable"):
        registry.start()
    assert not registry.started


def test_start_twice_is_an_error_and_use_before_start_is_refused(tmp_path: Path) -> None:
    registry, _, _ = make(tmp_path)
    with pytest.raises(RuntimeError, match="not started"):
        with registry.lock("r1"):
            registry.open_run("r1")
    registry.start()
    with pytest.raises(RuntimeError, match="already started"):
        registry.start()
    registry.close()


def test_clean_looking_file_is_fsynced_at_startup_under_crash(tmp_path: Path) -> None:
    path = seed(tmp_path, "r1.jsonl", lines(sample_record("r1", 0)))
    before = path.read_bytes()
    registry, rec, directory = make(tmp_path)
    report = registry.start()
    assert rec.events == [("fsync_file", len(before)), ("fsync_dir", directory)]  # file first, then the directory
    assert report.synced_files == ("r1.jsonl",) and report.repaired == () and path.read_bytes() == before
    registry.close()


def test_damaged_tail_is_repaired_then_fsynced_then_the_directory(tmp_path: Path) -> None:
    good = lines(sample_record("r1", 0))
    path = seed(tmp_path, "r1.jsonl", good + b'{"record_type":"samp')
    registry, rec, directory = make(tmp_path)
    report = registry.start()
    data = path.read_bytes()
    assert data.startswith(good + b'{"record_type":"samp\x00\n') and data.endswith(b"\n")  # original bytes stay a prefix
    assert report.repaired == ("r1.jsonl",)
    assert rec.events == [("fsync_file", len(data)), ("fsync_file", len(data)), ("fsync_dir", directory)]  # repair's own sync, explicit sync, directory
    result = read_evidence(path)
    assert len(result.errors) == 1 and [r["record_type"] for r in result.records] == ["sample", "repair"]
    registry.close()


def test_startup_scan_covers_rejections_and_ignores_non_evidence_names(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    (directory / ".coroner_probe").mkdir(parents=True)
    (directory / ".coroner_probe" / "x.jsonl").write_bytes(b"not evidence")
    (directory / LOCK_FILE_NAME).write_bytes(b"")
    (directory / "_other.jsonl").write_bytes(b"junk")
    (directory / "bad name.jsonl").write_bytes(b"junk")
    (directory / "notes.txt").write_bytes(b"junk")
    (directory / "sub.jsonl").mkdir()
    (directory / "_rejections.jsonl").write_bytes(b'{"half":')
    registry, _, _ = make(tmp_path)
    report = registry.start()
    assert report.evidence_files == ("_rejections.jsonl",) and report.repaired == ("_rejections.jsonl",)
    assert dict(report.skipped) == {"_other.jsonl": "not an evidence file name", "bad name.jsonl": "not an evidence file name", "sub.jsonl": "not a regular file"}
    assert (directory / "_other.jsonl").read_bytes() == b"junk" and (directory / "bad name.jsonl").read_bytes() == b"junk"
    assert (directory / ".coroner_probe" / "x.jsonl").read_bytes() == b"not evidence"
    repair = json.loads((directory / "_rejections.jsonl").read_bytes().split(b"\n")[1])
    assert repair["record_type"] == "repair" and repair["run_id"] is None and repair["file"] == "_rejections.jsonl"
    registry.close()


def test_startup_file_failure_gates_only_that_run_until_recovery_succeeds(tmp_path: Path) -> None:
    seed(tmp_path, "bad.jsonl", lines(sample_record("bad", 0)))
    seed(tmp_path, "good.jsonl", lines(sample_record("good", 0)))
    registry, rec, _ = make(tmp_path)
    rec.fail_file = True
    report = registry.start()  # every file fsync fails; the directory fsync works
    assert {name for name, _ in report.failed} == {"bad.jsonl", "good.jsonl"} and registry.started
    assert registry.get("bad").dirty and registry.get("good").dirty
    with registry.lock("bad"):
        with pytest.raises(OSError):
            registry.open_run("bad")
        assert registry.get("bad").dirty and not registry.get("bad").loaded
    rec.fail_file = False
    st = open_state(registry, "bad")
    assert st.loaded and not st.dirty and st.highest_seq == 0
    registry.close()


def test_directory_fsync_failure_aborts_startup_and_releases_the_lock(tmp_path: Path) -> None:
    class FakeLock:
        held = False

        def __init__(self) -> None:
            self.calls: list[str] = []

        def acquire(self) -> None:
            self.calls.append("acquire")

        def release(self) -> None:
            self.calls.append("release")

    lock = FakeLock()

    def broken_dir(path: Path) -> None:
        raise OSError("EIO")

    registry = RunRegistry(config_for(tmp_path / "evidence", Durability.CRASH), fsync_dir=broken_dir, volume_lock=lock)  # type: ignore[arg-type]
    with pytest.raises(CollectorStartupError, match="cannot fsync evidence directory"):
        registry.start()
    assert lock.calls == ["acquire", "release"] and not registry.started


def test_default_fsync_operations_work_on_this_platform(tmp_path: Path) -> None:
    seed(tmp_path, "r1.jsonl", lines(sample_record("r1", 0)))
    registry = RunRegistry(config_for(tmp_path / "evidence", Durability.CRASH), now=lambda: NOW)
    assert registry.start().synced_files == ("r1.jsonl",)  # real os.fsync; fsync_directory is a no-op on win32
    registry.close()


# --- open_run: creation and reopening --------------------------------------------------------------------


def test_new_run_writes_created_metadata_with_crash_durability(tmp_path: Path) -> None:
    registry, rec, directory = started(tmp_path)
    st = open_state(registry, "r1")
    path = directory / "r1.jsonl"
    (record,) = read_evidence(path).records
    assert validate_record(record) == []
    assert record == {
        "record_type": "run_metadata", "schema_version": 1, "run_id": "r1", "recv_time": NOW, "event": "created",
        "durability": "crash", "fsync_before_ack": True, "evidence_file": "r1.jsonl", "collector_version": COLLECTOR_VERSION,
    }
    assert rec.events == [("fsync_file", path.stat().st_size), ("fsync_dir", directory)]  # file, then directory entry
    assert st.loaded and not st.dirty and st.highest_seq is None and st.next_gap_number == 1
    registry.close()


def test_new_run_under_process_durability_claims_and_does_nothing_more(tmp_path: Path) -> None:
    registry, rec, directory = started(tmp_path, Durability.PROCESS)
    open_state(registry, "r1")
    (record,) = read_evidence(directory / "r1.jsonl").records
    assert (record["durability"], record["fsync_before_ack"], record["event"]) == ("process", False, "created")
    assert rec.events == []
    registry.close()


def test_open_run_is_idempotent_and_returns_the_same_state(tmp_path: Path) -> None:
    registry, rec, directory = started(tmp_path)
    first = open_state(registry, "r1")
    size, events = (directory / "r1.jsonl").stat().st_size, list(rec.events)
    assert open_state(registry, "r1") is first
    assert (directory / "r1.jsonl").stat().st_size == size and rec.events == events
    registry.close()


def test_existing_run_is_resumed_and_original_bytes_are_a_prefix(tmp_path: Path) -> None:
    original = lines(sample_record("r1", 0), sample_record("r1", 1, 250, "2025-01-01T12:00:02.000Z"))
    path = seed(tmp_path, "r1.jsonl", original)
    registry, _, _ = started(tmp_path)
    st = open_state(registry, "r1")
    data = path.read_bytes()
    assert data.startswith(original) and len(data) > len(original)
    meta = read_evidence(path).records[-1]
    assert meta["record_type"] == "run_metadata" and meta["event"] == "resumed" and validate_record(meta) == []
    assert (st.highest_seq, st.seen.ranges(), st.interval_ms, st.last_sample_recv_time) == (1, ((0, 1),), 250, "2025-01-01T12:00:02.000Z")
    assert st.last_sample_monotonic is None  # never fabricated from wall-clock time
    registry.close()


_FRAGMENT = serialize_record(sample_record("r1", 5))[:-1]  # unterminated Sample line


@pytest.mark.parametrize(
    "original",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not json at all\n", id="malformed-only"),
        pytest.param(_FRAGMENT, id="unterminated-fragment"),
        pytest.param(_FRAGMENT + b"\x00", id="fragment-plus-nul"),
        pytest.param(_FRAGMENT + b"\x00\n" + b'{"record_type": "repair", "schema', id="fragment-nul-partial-repair"),
        pytest.param(lines(sample_record("r1", 0)), id="valid"),
    ],
)
def test_existing_file_is_resumed_and_original_bytes_survive(tmp_path: Path, original: bytes) -> None:
    path = seed(tmp_path, "r1.jsonl", original)
    registry, _, _ = started(tmp_path)  # the startup scan repairs but never creates files
    after_start = path.read_bytes()
    assert after_start.startswith(original)
    open_state(registry, "r1")
    data = path.read_bytes()
    assert hashlib.sha256(data[: len(after_start)]).digest() == hashlib.sha256(after_start).digest()
    assert data.startswith(original)
    meta = read_evidence(path).records[-1]
    assert meta["record_type"] == "run_metadata" and meta["event"] == "resumed" and validate_record(meta) == []
    registry.close()


def test_existing_file_is_resumed_even_when_startup_scan_was_skipped(tmp_path: Path) -> None:
    # An empty file that appears after startup: open_run sees it exists, so the event is "resumed".
    registry, _, directory = started(tmp_path)
    path = seed(tmp_path, "r1.jsonl", b"")
    open_state(registry, "r1")
    assert [r["event"] for r in read_evidence(path).records] == ["resumed"] and directory.is_dir()
    registry.close()


def test_retry_after_failed_metadata_write_on_new_file_is_resumed(tmp_path: Path) -> None:
    registry, rec, directory = started(tmp_path)
    rec.fail_file = True
    with registry.lock("r1"):
        with pytest.raises(OSError):
            registry.open_run("r1")
    path = directory / "r1.jsonl"
    assert path.exists()  # the failed first write left the file behind
    rec.fail_file = False
    st = open_state(registry, "r1")
    assert st.loaded and not st.dirty
    assert [r["event"] for r in read_evidence(path).records] == ["created", "resumed"]
    registry.close()


def test_reconstruction_ignores_quarantined_unacknowledged_lines(tmp_path: Path) -> None:
    base = lines(sample_record("r1", 0), discontinuity_gap("r1", "gap-0002", 1, 1))
    gap99 = serialize_record(discontinuity_gap("r1", "gap-0099", 2, 2))[:-1]  # no newline: isolated by repair
    path = seed(tmp_path, "r1.jsonl", base + gap99)
    registry, _, _ = started(tmp_path)
    st = open_state(registry, "r1")
    assert st.next_gap_number == 3 and st.highest_seq == 0
    assert any(e.kind == "isolated_by_repair" for e in read_evidence(path).errors)
    registry.close()
    sample9 = serialize_record(sample_record("r1", 9))[:-1]
    seed(tmp_path, "r2.jsonl", lines(sample_record("r2", 0)) + sample9.replace(b'"r1"', b'"r2"'))
    registry2, _, _ = started(tmp_path)
    st2 = open_state(registry2, "r2")
    assert st2.highest_seq == 0 and st2.seen.ranges() == ((0, 0),)  # the unterminated Sample was never acknowledged
    registry2.close()


def test_restores_silence_open_and_terminated(tmp_path: Path) -> None:
    seed(tmp_path, "s.jsonl", lines(sample_record("s", 0), silence_gap("s")))
    seed(tmp_path, "t.jsonl", lines(sample_record("t", 0), termination_record("t")))
    registry, _, _ = started(tmp_path)
    s, t = open_state(registry, "s"), open_state(registry, "t")
    assert s.silence_open and not s.terminated and t.terminated and not t.silence_open
    registry.close()


def test_invalid_run_ids_are_refused(tmp_path: Path) -> None:
    registry, _, _ = started(tmp_path)
    for bad in ("", "_rejections", "../x", "a/b", "a" * 65, ".hidden"):
        with pytest.raises(ValueError):
            registry.get(bad)
    registry.close()


def test_open_run_requires_the_run_lock(tmp_path: Path) -> None:
    registry, _, _ = started(tmp_path)
    with pytest.raises(RuntimeError, match="run lock"):
        registry.open_run("r1")
    registry.close()


# --- recovery order and the replay hook ------------------------------------------------------------------


def test_replay_hook_runs_after_repair_and_before_the_rebuild_and_its_appends_are_counted(tmp_path: Path) -> None:
    seed(tmp_path, "r1.jsonl", lines(sample_record("r1", 0)))
    seen: dict = {}

    def hook(run_id: str, path: Path, read) -> None:
        seen["tail"] = path.read_bytes()[-1:]
        seen["loaded"] = registry.get(run_id).loaded
        seen["records"] = len(read.records)
        registry.writer.append(run_id, [discontinuity_gap(run_id, "gap-0004", 1, 1)])  # what 3.5 will do

    registry, _, _ = make(tmp_path, replay_hook=hook)
    registry.start()
    st = open_state(registry, "r1")
    assert seen == {"tail": b"\n", "loaded": False, "records": 1}
    assert st.next_gap_number == 5  # rebuilt after the hook
    registry.close()


def test_default_replay_hook_regenerates_nothing(tmp_path: Path) -> None:
    original = lines(sample_record("r1", 0), sample_record("r1", 3))  # a missing-gap situation 3.5 will repair
    path = seed(tmp_path, "r1.jsonl", original)
    registry, _, _ = started(tmp_path)
    open_state(registry, "r1")
    assert [r["record_type"] for r in read_evidence(path).records] == ["sample", "sample", "run_metadata"]
    registry.close()


def test_a_failing_hook_leaves_the_run_dirty(tmp_path: Path) -> None:
    seed(tmp_path, "r1.jsonl", lines(sample_record("r1", 0)))
    calls = []

    def hook(run_id: str, path: Path, read) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("replay failed")

    registry, _, _ = started(tmp_path, replay_hook=hook)
    with registry.lock("r1"):
        with pytest.raises(OSError):
            registry.open_run("r1")
    st = registry.get("r1")
    assert st.dirty and not st.loaded
    assert open_state(registry, "r1").loaded and not st.dirty
    registry.close()


# --- dirty state and failure injection -------------------------------------------------------------------


def test_mark_dirty_forces_tail_repair_and_fsync_before_the_next_append(tmp_path: Path) -> None:
    registry, rec, directory = started(tmp_path)
    st = open_state(registry, "r1")
    path = directory / "r1.jsonl"
    with registry.lock("r1"):
        registry.append(st, [sample_record("r1", 0)])
        st.apply(Outcome(new_sample=NewSample(0, RECV, 500, 1.0)))
        with open(path, "ab") as handle:
            handle.write(b'{"record_type":"sample","sch')  # a failed write left a partial line
        st.mark_dirty()
        with pytest.raises(RunDirtyError):
            registry.append(st, [sample_record("r1", 1)])
        rec.events.clear()
        assert registry.open_run("r1") is st
    data = path.read_bytes()
    assert not st.dirty and st.highest_seq == 0 and st.last_sample_monotonic == 1.0  # in-process monotonic is kept
    assert rec.events[-2:] == [("fsync_file", len(data)), ("fsync_dir", directory)]
    assert [r["record_type"] for r in read_evidence(path).records] == ["run_metadata", "sample", "repair"]
    assert sum(r["record_type"] == "run_metadata" for r in read_evidence(path).records) == 1  # no second metadata record
    with registry.lock("r1"):
        registry.append(st, [sample_record("r1", 1)])  # the append after recovery works on a fresh line
    assert read_evidence(path).records[-1]["sample"]["seq"] == 1
    registry.close()


def test_dirty_recovery_under_process_durability_does_not_fsync(tmp_path: Path) -> None:
    registry, rec, directory = started(tmp_path, Durability.PROCESS)
    st = open_state(registry, "r1")
    (directory / "r1.jsonl").write_bytes((directory / "r1.jsonl").read_bytes() + b"{")
    with registry.lock("r1"):
        st.mark_dirty()
        registry.open_run("r1")
    assert rec.events == [] and not st.dirty
    registry.close()


class FailingFile:
    def __init__(self, real, mode: str) -> None:
        self._real, self._mode = real, mode

    def __enter__(self) -> "FailingFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        if self._mode == "write":
            raise OSError("disk full")
        if self._mode == "partial":
            self._real.write(data[: len(data) // 2])
            raise OSError("disk full")
        return self._real.write(data)

    def flush(self) -> None:
        if self._mode == "flush":
            raise OSError("flush failed")
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()


@pytest.mark.parametrize("mode", ["write", "partial", "flush", "fsync"])
def test_failed_metadata_write_leaves_the_run_dirty_and_a_retry_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    registry, rec, directory = started(tmp_path)
    path = directory / "r1.jsonl"
    if mode == "fsync":
        rec.fail_file = True
    else:
        monkeypatch.setattr(writer_module, "open", lambda p, m: FailingFile(builtins.open(p, m), mode), raising=False)
    with registry.lock("r1"):
        with pytest.raises(OSError):
            registry.open_run("r1")
    st = registry.get("r1")
    assert st.dirty and not st.loaded and st.highest_seq is None and st.next_gap_number == 1
    with registry.lock("r1"):
        with pytest.raises(RunDirtyError):
            registry.append(st, [sample_record("r1", 0)])
    rec.fail_file = False
    monkeypatch.delattr(writer_module, "open", raising=False)
    rec.events.clear()
    with registry.lock("r1"):
        assert registry.open_run("r1") is st
    assert st.loaded and not st.dirty
    result = read_evidence(path)
    assert result.records[-1]["record_type"] == "run_metadata" and validate_record(result.records[-1]) == []
    if mode == "partial":
        assert len(result.errors) == 1 and result.records[-2]["record_type"] == "repair"
    # durability re-established: the directory was synced, and the whole file (metadata line included) was fsynced
    assert ("fsync_dir", directory) in rec.events and ("fsync_file", path.stat().st_size) in rec.events
    assert path.read_bytes().endswith(b"\n")
    registry.close()


@pytest.mark.parametrize("mode", ["write", "partial", "flush", "fsync"])
def test_failed_append_does_not_advance_state_and_the_retry_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    registry, rec, directory = started(tmp_path)
    st = open_state(registry, "r1")
    before = (st.highest_seq, st.seen.ranges(), st.next_gap_number, st.last_sample_monotonic)
    if mode == "fsync":
        rec.fail_file = True
    else:
        monkeypatch.setattr(writer_module, "open", lambda p, m: FailingFile(builtins.open(p, m), mode), raising=False)
    with registry.lock("r1"):
        with pytest.raises(OSError):
            registry.append(st, [sample_record("r1", 0)])
        assert st.dirty
        with pytest.raises(RunDirtyError):
            st.apply(Outcome(new_sample=NewSample(0, RECV, 500, 1.0)))
    assert (st.highest_seq, st.seen.ranges(), st.next_gap_number, st.last_sample_monotonic) == before
    rec.fail_file = False
    monkeypatch.delattr(writer_module, "open", raising=False)
    with registry.lock("r1"):
        registry.open_run("r1")
        registry.append(st, [sample_record("r1", 1)])
        st.apply(Outcome(new_sample=NewSample(1, RECV, 500, 2.0)))
    assert not st.dirty and st.highest_seq == 1 and (directory / "r1.jsonl").read_bytes().endswith(b"\n")
    registry.close()


def test_invalid_record_is_rejected_without_dirtying_the_run(tmp_path: Path) -> None:
    registry, _, directory = started(tmp_path)
    st = open_state(registry, "r1")
    size = (directory / "r1.jsonl").stat().st_size
    with registry.lock("r1"):
        with pytest.raises(ValueError):
            registry.append(st, [{"record_type": "sample"}])
    assert not st.dirty and (directory / "r1.jsonl").stat().st_size == size
    registry.close()


def test_append_requires_the_run_lock(tmp_path: Path) -> None:
    registry, _, _ = started(tmp_path)
    st = open_state(registry, "r1")
    with pytest.raises(RuntimeError, match="run lock"):
        registry.append(st, [sample_record("r1", 0)])
    registry.close()


def test_rejections_file_dirty_flag_is_retried(tmp_path: Path) -> None:
    seed(tmp_path, "_rejections.jsonl", b'{"half":')
    registry, rec, directory = make(tmp_path)
    rec.fail_file = True
    report = registry.start()
    assert report.failed and registry.rejections_dirty
    rec.fail_file = False
    registry.ensure_rejections_ready()
    assert not registry.rejections_dirty and (directory / "_rejections.jsonl").read_bytes().endswith(b"\n")
    registry.close()


# --- concurrency -----------------------------------------------------------------------------------------


def test_run_locks_are_independent_and_the_registry_guard_is_short(tmp_path: Path) -> None:
    registry, _, _ = started(tmp_path)
    a_held, release_a = threading.Event(), threading.Event()

    def hold_a() -> None:
        with registry.lock("a"):
            registry.open_run("a")
            registry.get("a").apply(Outcome(new_sample=NewSample(0, RECV, 500, 1.0)))
            a_held.set()
            release_a.wait(20)

    holder = threading.Thread(target=hold_a, daemon=True)
    holder.start()
    assert a_held.wait(20)
    done: list[RunState] = []

    def work_b() -> None:
        with registry.lock("b"):
            st = registry.open_run("b")
            registry.append(st, [sample_record("b", 0)])
            st.apply(Outcome(new_sample=NewSample(0, RECV, 500, 2.0), gap_ids=("gap-0003",)))
            done.append(st)
        registry.get("c")  # the registry guard is free while A is locked

    worker = threading.Thread(target=work_b, daemon=True)
    worker.start()
    worker.join(20)
    assert not worker.is_alive() and len(done) == 1  # B was not blocked by A's lock
    a = registry.get("a")
    assert a.lock.locked() and not a.lock.acquire(timeout=0.05)  # the same run is mutually exclusive
    assert a.last_sample_monotonic == 1.0 and a.next_gap_number == 1  # B's changes did not leak into A
    assert done[0].next_gap_number == 4 and done[0].last_sample_monotonic == 2.0
    release_a.set()
    holder.join(20)
    assert not a.lock.locked()
    registry.close()


def test_concurrent_get_returns_one_state_per_run(tmp_path: Path) -> None:
    registry, _, _ = started(tmp_path)
    results: list[RunState] = []
    barrier = threading.Barrier(8)

    def grab() -> None:
        barrier.wait(20)
        results.append(registry.get("r1"))

    threads = [threading.Thread(target=grab, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert len(results) == 8 and all(r is results[0] for r in results)
    registry.close()


# --- single-Collector lock -------------------------------------------------------------------------------


def test_lock_is_a_no_op_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    calls: list[int] = []
    lock = VolumeLock(tmp_path, flock=calls.append)
    lock.acquire()
    assert not lock.held and calls == [] and not (tmp_path / LOCK_FILE_NAME).exists()
    lock.release()
    lock.release()


def test_lock_maps_a_contended_flock_to_a_clear_error_and_closes_the_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    fds: list[int] = []

    def contended(fd: int) -> None:
        fds.append(fd)
        raise BlockingIOError(11, "Resource temporarily unavailable")

    lock = VolumeLock(tmp_path, flock=contended)
    with pytest.raises(CollectorAlreadyRunning, match=r"another Collector already holds .*\.collector\.lock"):
        lock.acquire()
    assert not lock.held
    with pytest.raises(OSError):
        os.fstat(fds[0])  # closed
    assert (tmp_path / LOCK_FILE_NAME).exists()


def test_lock_other_errors_are_startup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    def broken(fd: int) -> None:
        raise OSError(5, "I/O error")

    with pytest.raises(CollectorStartupError, match="cannot lock") as info:
        VolumeLock(tmp_path, flock=broken).acquire()
    assert not isinstance(info.value, CollectorAlreadyRunning)


def test_lock_acquire_release_are_idempotent_and_keep_the_lock_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    taken: list[int] = []
    lock = VolumeLock(tmp_path, flock=taken.append)
    lock.acquire()
    lock.acquire()
    assert lock.held and len(taken) == 1
    fd = taken[0]
    lock.release()
    lock.release()
    assert not lock.held
    with pytest.raises(OSError):
        os.fstat(fd)
    assert (tmp_path / LOCK_FILE_NAME).exists()  # never deleted


def test_registry_close_releases_the_lock_and_is_idempotent(tmp_path: Path) -> None:
    class FakeLock:
        held = True

        def __init__(self) -> None:
            self.released = 0

        def acquire(self) -> None:
            pass

        def release(self) -> None:
            self.released += 1

    lock = FakeLock()
    registry = RunRegistry(config_for(tmp_path / "evidence", Durability.PROCESS), volume_lock=lock)  # type: ignore[arg-type]
    registry.start()
    registry.close()
    registry.close()
    assert lock.released == 2 and not registry.started


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl.flock exists only on POSIX; on win32 the lock is a documented no-op")
def test_second_collector_on_the_same_volume_fails_fast(tmp_path: Path) -> None:
    first, _, directory = make(tmp_path, Durability.PROCESS)
    first.start()
    second, _, _ = make(tmp_path, Durability.PROCESS)
    with pytest.raises(CollectorAlreadyRunning):
        second.start()
    first.close()
    third, _, _ = make(tmp_path, Durability.PROCESS)
    third.start()  # the lock was released, and the lock file was kept
    assert (directory / LOCK_FILE_NAME).exists()
    third.close()


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl.flock exists only on POSIX")
def test_real_flock_blocks_a_second_holder_until_release(tmp_path: Path) -> None:
    one, two = VolumeLock(tmp_path), VolumeLock(tmp_path)
    one.acquire()
    with pytest.raises(CollectorAlreadyRunning):
        two.acquire()
    one.release()
    two.acquire()
    assert two.held
    two.release()
