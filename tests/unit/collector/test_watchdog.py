"""Task 3.9: SilenceWatchdog (design "Silence-timeout watchdog", D29 to D33; Requirements 6.2, 6.13).

A fake monotonic clock and a fake wall clock drive `check_once()` directly, so nothing sleeps. Only the lifecycle test
runs the real thread (tiny tick, Event synchronisation). Storage failures are injected through the writer's `open`
seam, as in test_service.py.
"""
from __future__ import annotations

import builtins
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from coroner.collector.config import CollectorConfig
from coroner.collector.service import IngestService, create_ingest_service
from coroner.collector.state import NewSample, Outcome
from coroner.collector.watchdog import SilenceWatchdog, create_watchdog
from coroner.common.timeutil import format_utc_ms
from coroner.evidence import writer as writer_module
from coroner.evidence.reader import read_evidence
from coroner.evidence.schema import validate_record
from coroner.evidence.writer import Durability

BASE = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TERMINATION = {
    "source": "experiment_runner", "observed_time": "2025-01-01T12:17:03.221Z",
    "container_state": {"status": "exited", "exit_code": 137, "oom_killed": True},
    "cause_label": "docker_oom_kill", "note": "Observed after the fact via docker inspect.",
}


class Mono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Clock:
    def __init__(self) -> None:
        self.ticks = 0
        self.lock = threading.Lock()

    def now(self) -> str:
        with self.lock:
            self.ticks += 1
            return format_utc_ms(BASE + timedelta(milliseconds=10 * self.ticks))


class FakeProbe:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1


class FailingFile:
    def __init__(self, real: Any, mode: str, limit: int | None) -> None:
        self._real, self._mode, self._limit = real, mode, limit

    def __enter__(self) -> "FailingFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        if self._mode == "write":
            raise OSError("disk full")
        self._real.write(data[: self._limit if self._limit is not None else len(data) // 2])
        raise OSError("disk full")

    def flush(self) -> None:
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()


class Faults:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.mode: str | None = None
        self.name = ""
        self.limit: int | None = None
        monkeypatch.setattr(writer_module, "open", self._open, raising=False)

    def arm(self, mode: str, name: str, limit: int | None = None) -> None:
        self.mode, self.name, self.limit = mode, name, limit

    def disarm(self) -> None:
        self.mode = None

    def _open(self, path: Any, mode: str) -> Any:
        real = builtins.open(path, mode)
        if self.mode is None or Path(path).name != self.name:
            return real
        mode_now, self.mode = self.mode, None  # one failure
        return FailingFile(real, mode_now, self.limit)


def sample(run_id: str, seq: int, interval_ms: int = 500, dropped: int = 0) -> bytes:
    return json.dumps({
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 1000, "request_count": 1,
        "loop_delay_ms": 0.5, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": interval_ms, "dropped": dropped,
    }).encode()


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.directory = tmp_path / "evidence"
        self.config = CollectorConfig(Durability.CRASH, 65536, 50, self.directory)
        self.mono = Mono()
        self.clock = Clock()
        self.probe = FakeProbe()
        self.service: IngestService
        self.wd: SilenceWatchdog
        self.build()

    def build(self) -> None:
        self.service = create_ingest_service(
            self.config, fsync=lambda fd: None, fsync_dir=lambda p: None, now=self.clock.now,
            monotonic=self.mono, storage_probe=self.probe,
        )
        self.service.registry.start()
        self.wd = create_watchdog(self.service)

    def restart(self) -> None:
        self.service.registry.close()
        self.build()

    def ingest(self, run_id: str = "r1", seq: int = 0, interval_ms: int = 500, dropped: int = 0) -> Any:
        return self.service.ingest(sample(run_id, seq, interval_ms, dropped))

    def path(self, run_id: str = "r1") -> Path:
        return self.directory / f"{run_id}.jsonl"

    def records(self, run_id: str = "r1") -> list[dict[str, Any]]:
        return list(read_evidence(self.path(run_id)).records)

    def silences(self, run_id: str = "r1") -> list[dict[str, Any]]:
        return [r for r in self.records(run_id) if r["record_type"] == "gap" and r["gap_type"] == "silence_timeout"]

    def state(self, run_id: str = "r1") -> Any:
        return self.service.registry.get(run_id)

    def sha(self, run_id: str) -> str:
        return hashlib.sha256(self.path(run_id).read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# --- which runs, threshold, timing -----------------------------------------------------------------------


def test_no_runs_means_no_work(env: Env) -> None:
    env.mono.t = 1000.0
    assert env.wd.check_once() == []


def test_historical_run_after_restart_is_not_monitored(env: Env) -> None:
    assert env.ingest().http_status == 200
    env.restart()
    with env.service.registry.lock("r1"):
        env.service.registry.open_run("r1")
    assert env.state().last_sample_monotonic is None
    env.mono.t = 10_000.0
    assert env.wd.check_once() == []
    assert env.silences() == []


def test_a_newly_received_sample_is_monitored(env: Env) -> None:
    env.ingest()
    assert env.state().last_sample_monotonic == 0.0
    env.mono.t = 1.5001
    assert env.wd.check_once() == ["r1:gap-0001"]


@pytest.mark.parametrize("interval_ms", [100, 500, 60000])
def test_exact_threshold_is_not_silence_and_just_after_is(env: Env, interval_ms: int) -> None:
    env.ingest(interval_ms=interval_ms)
    threshold_s = 3 * interval_ms / 1000.0
    env.mono.t = threshold_s - 0.001
    assert env.wd.check_once() == []
    env.mono.t = threshold_s  # exactly 3 * interval: not silent
    assert env.wd.check_once() == []
    assert env.silences() == []
    env.mono.t = threshold_s + 0.001  # first eligible tick after
    assert env.wd.check_once() == ["r1:gap-0001"]
    gap = env.silences()[0]
    assert gap["threshold_ms"] == 3 * interval_ms and gap["interval_ms"] == interval_ms


def test_gap_record_fields_follow_the_design(env: Env) -> None:
    first = env.ingest()
    env.mono.t = 2.0
    env.wd.check_once()
    gap = env.silences()[0]
    assert validate_record(gap) == []
    assert gap["gap_id"] == "gap-0001" and gap["sample_count"] == "unknown"
    assert gap["start_time"] == gap["last_sample_time"] == first.recv_time
    assert gap["recv_time"] == gap["end_time"] == gap["detected_time"] != first.recv_time
    assert (gap["threshold_ms"], gap["interval_ms"]) == (1500, 500)
    assert gap["reconciliation"] == "not_applicable" and gap["reconciles_with"] == []
    assert not {"first_missing_seq", "last_missing_seq", "missing_count", "detected_at_seq"} & set(gap)
    state = env.state()
    assert state.silence_open is True and state.next_gap_number == 2


def test_the_interval_comes_from_the_last_accepted_sample(env: Env) -> None:
    env.ingest(seq=0, interval_ms=500)
    env.ingest(seq=1, interval_ms=100)
    env.mono.t = 0.31
    assert env.wd.check_once() == ["r1:gap-0001"]
    assert env.silences()[0]["interval_ms"] == 100


# --- one gap per silence period ---------------------------------------------------------------------------


def test_many_ticks_in_one_silence_make_one_gap(env: Env) -> None:
    env.ingest()
    for step in range(2, 40):
        env.mono.t = step * 0.5
        env.wd.check_once()
    assert len(env.silences()) == 1


def test_a_sample_closes_the_silence_and_a_second_period_gets_the_next_id(env: Env) -> None:
    env.ingest(seq=0)
    env.mono.t = 2.0
    env.wd.check_once()
    env.ingest(seq=1)  # accepted at t=2.0
    assert env.state().silence_open is False
    env.mono.t = 3.5  # exactly 3 * interval after the new Sample
    assert env.wd.check_once() == []
    env.mono.t = 3.6
    assert env.wd.check_once() == ["r1:gap-0002"]
    assert [g["gap_id"] for g in env.silences()] == ["gap-0001", "gap-0002"]


def test_gap_ids_are_unique_across_ingest_and_silence_gaps(env: Env) -> None:
    env.ingest(seq=0)
    env.ingest(seq=3)  # discontinuity gap-0001
    env.mono.t = 2.0
    env.wd.check_once()  # gap-0002
    env.ingest(seq=7)  # discontinuity gap-0003, closes silence
    env.mono.t = 4.0
    env.wd.check_once()  # gap-0004
    ids = [r["gap_id"] for r in env.records() if r["record_type"] == "gap"]
    assert ids == ["gap-0001", "gap-0002", "gap-0003", "gap-0004"]
    assert read_evidence(env.path()).errors == []


# --- termination ------------------------------------------------------------------------------------------


def test_a_terminated_run_never_gets_a_silence_gap(env: Env) -> None:
    env.ingest()
    assert env.service.record_termination("r1", json.dumps(TERMINATION).encode()).http_status == 200
    env.mono.t = 100.0
    assert env.wd.check_once() == [] and env.silences() == []


def test_a_late_sample_after_termination_never_reactivates_monitoring(env: Env) -> None:
    env.ingest(seq=0)
    env.service.record_termination("r1", json.dumps(TERMINATION).encode())
    env.mono.t = 1.0
    assert env.ingest(seq=1).http_status == 200
    state = env.state()
    assert state.terminated and state.last_sample_monotonic == 1.0  # apply does set it; terminated wins
    env.mono.t = 500.0
    assert env.wd.check_once() == [] and env.silences() == []


def test_unrelated_run_files_are_not_touched(env: Env) -> None:
    env.ingest("r2")
    env.service.record_termination("r2", json.dumps(TERMINATION).encode())
    before = env.sha("r2")
    env.ingest("r1")
    env.mono.t = 5.0
    assert env.wd.check_once() == ["r1:gap-0001"]
    assert env.sha("r2") == before


# --- failure and recovery ---------------------------------------------------------------------------------


def test_append_failure_leaves_state_clean_marks_dirty_and_other_runs_are_served(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    faults = Faults(monkeypatch)
    env.ingest("r1")
    env.ingest("r2")
    env.mono.t = 2.0
    faults.arm("write", "r1.jsonl")
    assert env.wd.check_once() == ["r2:gap-0001"]  # r1 failed, r2 still served in the same pass
    state = env.state("r1")
    assert state.silence_open is False and state.dirty is True and state.next_gap_number == 1
    assert env.probe.invalidations == 1
    assert env.silences("r1") == []


def test_the_next_tick_recovers_and_writes_exactly_one_valid_gap(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    faults = Faults(monkeypatch)
    env.ingest()
    env.mono.t = 2.0
    faults.arm("partial", "r1.jsonl", limit=60)
    assert env.wd.check_once() == []
    assert env.wd.check_once() == ["r1:gap-0001"]  # open_run repaired the tail, then the retry succeeded
    assert env.wd.check_once() == []
    gaps = env.silences()
    assert len(gaps) == 1 and validate_record(gaps[0]) == []
    result = read_evidence(env.path())
    assert len(result.errors) >= 1 or any(r["record_type"] == "repair" for r in result.records)
    assert [r["record_type"] for r in result.records].count("repair") == 1
    assert env.state().dirty is False and env.state().silence_open is True


def test_the_next_ingest_recovers_a_failed_silence_write(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    faults = Faults(monkeypatch)
    env.ingest()
    env.mono.t = 2.0
    faults.arm("partial", "r1.jsonl", limit=40)
    env.wd.check_once()
    assert env.ingest(seq=1).http_status == 200  # recovers, stores the Sample
    assert env.silences() == [] and env.state().dirty is False
    env.mono.t = 3.0
    assert env.wd.check_once() == []  # the new Sample restarted the clock at 2.0
    env.mono.t = 3.6
    assert env.wd.check_once() == ["r1:gap-0001"]  # the fragment was not counted: the id is reused safely
    assert len(env.silences()) == 1


def test_a_write_that_landed_despite_the_error_is_not_duplicated(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    faults = Faults(monkeypatch)
    env.ingest()
    env.mono.t = 2.0
    faults.arm("partial", "r1.jsonl", limit=10_000)  # whole line written, then an error: the gap is durable
    assert env.wd.check_once() == []
    assert env.state().dirty is True and env.state().silence_open is False
    assert env.wd.check_once() == []  # recovery rebuilds silence_open from the file: nothing more written
    assert len(env.silences()) == 1 and env.state().silence_open is True


# --- restart ----------------------------------------------------------------------------------------------


def test_a_restart_neither_regenerates_nor_duplicates_the_silence_gap(env: Env) -> None:
    env.ingest(seq=0)
    env.mono.t = 2.0
    env.wd.check_once()
    env.restart()
    env.mono.t = 4.0
    assert env.ingest(seq=1).http_status == 200  # open_run replays gaps; silence gaps are ignored by replay
    assert len(env.silences()) == 1
    env.mono.t = 10.0
    assert env.wd.check_once() == ["r1:gap-0002"]  # a new silence period after the new Sample
    assert len(env.silences()) == 2


# --- concurrency ------------------------------------------------------------------------------------------


def test_a_sample_arriving_before_the_lock_is_taken_wins(env: Env) -> None:
    env.ingest()
    env.mono.t = 2.0
    state = env.state()
    results: list[list[str]] = []
    state.lock.acquire()
    thread = threading.Thread(target=lambda: results.append(env.wd.check_once()))
    thread.start()
    try:
        state.apply(Outcome(new_sample=NewSample(1, "2025-01-01T12:00:09.000Z", 500, 2.0)))  # Sample accepted meanwhile
    finally:
        state.lock.release()
    thread.join(10)
    assert not thread.is_alive() and results == [[]]
    assert env.silences() == []


def test_two_concurrent_checks_write_one_gap(env: Env) -> None:
    env.ingest()
    env.mono.t = 2.0
    barrier = threading.Barrier(2)
    results: list[list[str]] = []

    def run() -> None:
        barrier.wait(10)
        results.append(env.wd.check_once())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(len(r) for r in results) == [0, 1]
    assert len(env.silences()) == 1


def test_the_registry_dict_lock_is_not_held_during_io(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.ingest()
    env.mono.t = 2.0
    registry = env.service.registry
    seen: list[bool] = []
    real_append = registry.append

    def spy(state: Any, records: Any) -> Any:
        seen.append(registry._guard.locked())
        return real_append(state, records)

    monkeypatch.setattr(registry, "append", spy)
    assert env.wd.check_once() == ["r1:gap-0001"]
    assert seen == [False]


# --- lifecycle --------------------------------------------------------------------------------------------


def test_thread_lifecycle_start_stop_are_idempotent_and_detection_runs(env: Env) -> None:
    registry = env.service.registry
    done = threading.Event()
    real_append = registry.append

    def signalling(state: Any, records: Any) -> Any:
        out = real_append(state, records)
        done.set()
        return out

    registry.append = signalling  # type: ignore[method-assign]
    env.ingest()
    env.mono.t = 2.0
    wd = SilenceWatchdog(registry, monotonic=env.mono, now=env.clock.now, tick_ms=1)
    wd.stop()  # before start: harmless
    wd.start()
    first = wd._thread
    wd.start()
    assert wd._thread is first and first is not None and first.daemon and wd.running
    assert done.wait(10)
    wd.stop(5)
    wd.stop(5)
    assert not wd.running and not first.is_alive()
    assert len(env.silences()) == 1


def test_the_loop_survives_a_failing_pass(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = threading.Event()
    count = {"n": 0}

    def boom() -> list[str]:
        count["n"] += 1
        if count["n"] >= 3:
            calls.set()
        raise RuntimeError("boom")

    wd = SilenceWatchdog(env.service.registry, monotonic=env.mono, now=env.clock.now, tick_ms=1)
    monkeypatch.setattr(wd, "check_once", boom)
    wd.start()
    try:
        assert calls.wait(10)
    finally:
        wd.stop(5)
    assert not wd.running


def test_tick_defaults_to_the_configured_value_and_must_be_positive(env: Env) -> None:
    assert env.wd._tick_s == 0.05
    with pytest.raises(ValueError):
        SilenceWatchdog(env.service.registry, tick_ms=0)
