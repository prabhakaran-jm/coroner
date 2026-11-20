"""Property 5: Run files are isolated by Run_Identifier (Task 3.6).

**Validates: Requirements 4.2, 4.7, 5.2, 7.5, 12.1**

Contract tested through the real `IngestService` and real files: every record for a run is appended only to
`<run_id>.jsonl`; distinct run ids map to distinct files; no file holds a record whose `run_id` differs from the
file's; a rejection that names a valid `run_id` stays in that run's file and every other rejection goes to
`_rejections.jsonl` with a null `run_id`; sequence state and gap numbering are independent per run (`gap-0001` may
appear in several files); work on one run never alters another run's bytes; a restart keeps every run's evidence and
rebuilds each run's state alone.

Independent oracle (`Model`): a small simulation written here that classifies each Sample per run with plain Python
sets and arithmetic. It does not import or call the classification, gap or record-building code of the Collector. It
predicts, per file, the exact ordered list of record signatures, and per run the highest seq, the seen set and the
next gap number.

Concurrency tests record the order in which appends really completed (a wrapper around `registry.append`, which the
service calls while holding the run lock) and apply the model to that serialized order, not to seq order.

Platform note: the strategy yields run ids that stay distinct after `str.lower()` and avoid Windows device names, so
the tests do not depend on filesystem case sensitivity. The product keeps the run id as given; two ids that differ
only by case would share a file on a case-insensitive filesystem (the Collector runs on Linux).
"""
from __future__ import annotations

import random
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from coroner.collector.config import CollectorConfig
from coroner.collector.service import IngestService, create_ingest_service
from coroner.common.timeutil import format_utc_ms
from coroner.evidence.reader import read_evidence
from coroner.evidence.writer import Durability

BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)
MAX_BODY = 2048
REJECTIONS = "_rejections"
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


# --- independent oracle ------------------------------------------------------------------------------------


class RunModel:
    def __init__(self) -> None:
        self.highest: int | None = None
        self.seen: set[int] = set()
        self.gap_next = 1
        self.sigs: list[tuple[Any, ...]] = []
        self.ever_written = False


class Model:
    """What every file and every run's state must be after a sequence of operations (no Collector code used)."""

    def __init__(self) -> None:
        self.runs: dict[str, RunModel] = {}
        self.rejections: list[tuple[Any, ...]] = []
        self.opened: set[str] = set()  # runs the current service instance has opened

    def restart(self) -> None:
        self.opened.clear()

    def _touch(self, run: str) -> list[tuple[Any, ...]]:
        model = self.runs.setdefault(run, RunModel())
        out: list[tuple[Any, ...]] = []
        if run not in self.opened:
            self.opened.add(run)
            out.append(("run_metadata", "resumed" if model.ever_written else "created"))
            model.ever_written = True
        return out

    def _add(self, run: str, sigs: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        self.runs[run].sigs.extend(sigs)
        return sigs

    def sample(self, run: str, seq: int, dropped: int) -> tuple[str, list[tuple[Any, ...]]]:
        out = self._touch(run)
        m = self.runs[run]
        if m.highest is not None and (seq in m.seen or seq == m.highest):
            out.append(("sequence_anomaly", "duplicate", seq, m.highest))
        elif m.highest is not None and seq < m.highest:
            out.append(("sequence_anomaly", "reset", seq, m.highest))
        else:
            expected = 0 if m.highest is None else m.highest + 1
            out.append(("sample", seq))
            if seq > expected:
                out.append(("gap", f"gap-{m.gap_next:04d}", "sequence_discontinuity", seq, expected, seq - 1))
                m.gap_next += 1
            if dropped > 0:
                first = seq - dropped
                if first < 0 or any(n in m.seen for n in range(first, seq)):
                    status, rng = "unknown", (None, None)
                else:
                    status, rng = "known", (first, seq - 1)
                out.append(("gap", f"gap-{m.gap_next:04d}", "probe_reported_drop", seq, dropped, status, *rng))
                m.gap_next += 1
            m.seen.add(seq)
            m.highest = seq if m.highest is None else max(m.highest, seq)
        return run, self._add(run, out)

    def rejection(self, run: str | None, status: int, code: str) -> tuple[str, list[tuple[Any, ...]]]:
        sig = ("rejection", run, status, code)
        if run is None:
            self.rejections.append(sig)
            return REJECTIONS, [sig]
        out = self._touch(run)
        out.append(sig)
        return run, self._add(run, out)


def signature(record: dict[str, Any]) -> tuple[Any, ...]:
    """The comparable facts of one stored record (the same shape the Model predicts)."""
    kind = record["record_type"]
    if kind == "run_metadata":
        return (kind, record["event"])
    if kind == "sample":
        return (kind, record["sample"]["seq"])
    if kind == "sequence_anomaly":
        return (kind, record["anomaly"], record["seq"], record["previous_seq"])
    if kind == "gap":
        if record["gap_type"] == "sequence_discontinuity":
            return (kind, record["gap_id"], record["gap_type"], record["detected_at_seq"], record["first_missing_seq"], record["last_missing_seq"])
        return (kind, record["gap_id"], record["gap_type"], record["detected_at_seq"], record["dropped_count"],
                record["range_status"], record["first_missing_seq"], record["last_missing_seq"])
    if kind == "rejection":
        return (kind, record["run_id"], record["http_status"], record["reason_code"])
    raise AssertionError(f"unexpected record type {kind}")


# --- harness -----------------------------------------------------------------------------------------------


class Clock:
    """One wall clock for the whole service, thread-safe, 1 ms per reading."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()
        self._local = threading.local()

    def now(self) -> str:
        with self._lock:
            self._n += 1
            value = format_utc_ms(BASE + timedelta(milliseconds=self._n))
        if getattr(self._local, "pending", False):  # the first reading of a tracked request is its arrival
            self._local.pending = False
            self._local.arrival = value
        return value

    def expect_arrival(self) -> None:
        self._local.pending = True
        self._local.arrival = None

    def arrival(self) -> str | None:
        return self._local.arrival


class Env:
    def __init__(self, root: Path) -> None:
        self.directory = root / "evidence"
        self.config = CollectorConfig(durability=Durability.CRASH, max_body_bytes=MAX_BODY, watchdog_tick_ms=50, evidence_dir=self.directory)
        self.clock = Clock()
        self.tracked: list[tuple[str, int, str, Any]] = []  # (run_id, seq, arrival clock value, result), any order
        self.service = self._build()

    def ingest_tracked(self, run_id: str, seq: int, dropped: int = 0) -> Any:
        """Ingest one Sample and record the clock value read when the request arrived (its first clock reading)."""
        self.clock.expect_arrival()
        result = self.service.ingest(raw_of(make_sample(run_id, seq, dropped)))
        arrival = self.clock.arrival()
        assert arrival is not None
        self.tracked.append((run_id, seq, arrival, result))
        return result

    def _build(self) -> IngestService:
        service = create_ingest_service(self.config, fsync=lambda fd: None, fsync_dir=lambda path: None, now=self.clock.now)
        service.registry.start()
        return service

    def restart(self) -> None:
        self.service.registry.close()
        self.service = self._build()

    def close(self) -> None:
        self.service.registry.close()

    def path(self, name: str) -> Path:
        return self.directory / f"{name}.jsonl"

    def snapshot(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(self.directory.glob("*.jsonl"))}

    def sigs(self, name: str) -> list[tuple[Any, ...]]:
        result = read_evidence(self.path(name))
        assert result.errors == [], result.errors
        return [signature(r) for r in result.records]


def make_sample(run_id: str, seq: int, dropped: int = 0) -> dict[str, Any]:
    return {
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 1000, "request_count": 1,
        "loop_delay_ms": 0.5, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": dropped,
    }


def raw_of(obj: object) -> bytes:
    import json

    return json.dumps(obj).encode()


# kind -> (builder of raw body, expected (status, code), whether the body names a valid run id)
def bad_with_valid_run(run_id: str, kind: str) -> tuple[bytes, int, str]:
    sample = make_sample(run_id, 0)
    if kind == "bad_ts":
        sample["ts"] = "nope"
        return raw_of(sample), 422, "invalid_format"
    if kind == "no_pid":
        del sample["pid"]
        return raw_of(sample), 422, "missing_field"
    if kind == "extra":
        sample["zzz"] = 1
        return raw_of(sample), 422, "unknown_field"
    sample["seq"] = -1  # neg_seq
    return raw_of(sample), 422, "out_of_range"


def bad_without_run(kind: str, other_run: str) -> tuple[bytes, int, str]:
    if kind == "bad_run":
        return raw_of(make_sample("bad id!", 0)), 422, "invalid_run_id"
    if kind == "reserved_run":
        return raw_of(make_sample(REJECTIONS, 0)), 422, "invalid_run_id"
    if kind == "no_run":
        sample = make_sample("x", 0)
        del sample["run_id"]
        return raw_of(sample), 422, "invalid_run_id"
    if kind == "bad_json":
        return b"{", 422, "invalid_json"
    if kind == "array":
        return b"[1]", 422, "wrong_type"
    # huge: names a valid run but is over the size limit, so it is refused before the run id is read
    return b'{"run_id":"' + other_run.encode() + b'","pad":"' + b"x" * (MAX_BODY + 10) + b'"}', 413, "body_too_large"


BAD_VALID = ["bad_ts", "no_pid", "extra", "neg_seq"]
BAD_NOVALID = ["bad_run", "reserved_run", "no_run", "bad_json", "array", "huge"]


def apply_op(env: Env, model: Model, runs: list[str], op: tuple[Any, ...]) -> tuple[str | None, list[tuple[Any, ...]]]:
    """Run one operation on the real service and the model. Returns (file stem that must change, its new signatures)."""
    kind = op[0]
    if kind == "restart":
        env.restart()
        model.restart()
        return None, []
    if kind == "sample":
        run = runs[op[1] % len(runs)]
        result = env.service.ingest(raw_of(make_sample(run, op[2], op[3])))
        assert result.http_status == 200, result.body
        return model.sample(run, op[2], op[3])
    if kind == "bad_valid":
        run = runs[op[1] % len(runs)]
        raw, status, code = bad_with_valid_run(run, op[2])
        result = env.service.ingest(raw)
        assert (result.http_status, result.body["reason_code"]) == (status, code), result.body
        return model.rejection(run, status, code)
    raw, status, code = bad_without_run(op[1], runs[0])
    result = env.service.ingest(raw)
    assert (result.http_status, result.body["reason_code"]) == (status, code), result.body
    return model.rejection(None, status, code)


def check_state(env: Env, model: Model) -> None:
    """Per-run state of every run the current service instance has opened equals the model's."""
    for run in model.opened:
        m = model.runs[run]
        state = env.service.registry.get(run)
        assert state.highest_seq == m.highest, run
        assert state.next_gap_number == m.gap_next, run
        assert {n for n in range(0, 40) if n in state.seen} == m.seen, run
        assert state.seen.size() == len(m.seen), run


def check_files(env: Env, model: Model, *, ordered: bool = True) -> None:
    """Every file holds exactly the records the model predicts, each with the file's own run_id; no stray files.

    `ordered` asserts recv_time increases in file order. That holds for one thread (a request reads the clock and
    persists before the next starts); with concurrent requests recv_time is the arrival time and need not increase.
    """
    names = {p.stem for p in env.directory.glob("*.jsonl")}
    expected = {run for run, m in model.runs.items() if m.sigs} | ({REJECTIONS} if model.rejections else set())
    assert names == expected, (names, expected)
    for run, m in model.runs.items():
        if m.sigs:
            assert env.sigs(run) == m.sigs, run
            result = read_evidence(env.path(run))
            assert all(r["run_id"] == run for r in result.records), f"{run}.jsonl holds another run's record"
            # run_metadata is stamped when the run is opened, after the receive time of the request that opened it
            times = [r["recv_time"] for r in result.records if r["record_type"] != "run_metadata"]
            if ordered:
                assert times == sorted(times)
    if model.rejections:
        result = read_evidence(env.path(REJECTIONS))
        assert [signature(r) for r in result.records] == model.rejections
        assert all(r["run_id"] is None and r["record_type"] == "rejection" for r in result.records)


def run_scenario(runs: list[str], ops: list[tuple[Any, ...]]) -> None:
    assert len({r.lower() for r in runs}) == len(runs)
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        model = Model()
        try:
            for op in ops:
                before = env.snapshot()
                target, sigs = apply_op(env, model, runs, op)
                after = env.snapshot()
                if target is None:  # a restart changes no byte
                    assert after == before
                else:
                    name = f"{target}.jsonl"
                    assert set(after) - set(before) <= {name}, "an unexpected file appeared"
                    for other in before:
                        if other != name:
                            assert after[other] == before[other], f"{other} changed while {name} was the target"
                    assert after[name].startswith(before.get(name, b"")), f"{name} was rewritten, not appended"
                    assert after[name].endswith(b"\n") and len(after[name]) > len(before.get(name, b""))
                    assert env.sigs(target)[-len(sigs):] == sigs, "the appended records differ from the model's"
                check_state(env, model)
            check_files(env, model)
            # recovery: a fresh service rebuilds each run alone and only appends its own `resumed` metadata
            env.restart()
            model.restart()
            for run in list(model.runs):
                if not model.runs[run].sigs:
                    continue
                before = env.snapshot()
                with env.service.registry.lock(run):
                    env.service.registry.open_run(run)
                model._add(run, model._touch(run))
                after = env.snapshot()
                for other in before:
                    if other != f"{run}.jsonl":
                        assert after[other] == before[other], f"recovering {run} changed {other}"
                assert after[f"{run}.jsonl"].startswith(before[f"{run}.jsonl"])
                check_state(env, model)
            check_files(env, model)
        finally:
            env.close()


# --- strategies --------------------------------------------------------------------------------------------

_FIRST = "aAbB01"
_REST = "aAbB01._-"
small_ids = (
    st.tuples(st.sampled_from(_FIRST), st.text(alphabet=_REST, min_size=0, max_size=5))
    .map(lambda t: t[0] + t[1])
    .filter(lambda s: s.split(".")[0].lower() not in _WINDOWS_DEVICES)
)
run_id_lists = st.lists(st.one_of(small_ids, st.just("A" + "x" * 63)), min_size=2, max_size=4, unique_by=str.lower)
seq_values = st.one_of(st.integers(0, 6), st.integers(0, 12))
dropped_values = st.one_of(st.just(0), st.just(0), st.integers(1, 4), st.integers(0, 15))
operations = st.one_of(
    st.tuples(st.just("sample"), st.integers(0, 3), seq_values, dropped_values),
    st.tuples(st.just("sample"), st.integers(0, 3), seq_values, dropped_values),
    st.tuples(st.just("sample"), st.integers(0, 3), seq_values, dropped_values),
    st.tuples(st.just("bad_valid"), st.integers(0, 3), st.sampled_from(BAD_VALID)),
    st.tuples(st.just("bad_novalid"), st.sampled_from(BAD_NOVALID)),
    st.tuples(st.just("restart")),
)
SETTINGS = settings(
    max_examples=150, deadline=None, derandomize=True, database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much],
)


@SETTINGS
@given(run_id_lists, st.lists(operations, min_size=1, max_size=35))
def test_property_5_run_files_are_isolated_by_run_identifier(runs: list[str], ops: list[tuple[Any, ...]]) -> None:
    run_scenario(runs, ops)


# --- explicit scenarios -------------------------------------------------------------------------------------


def test_gap_zero_one_appears_in_every_run_file_and_runs_do_not_share_state() -> None:
    ops: list[tuple[Any, ...]] = [("sample", 0, 3, 0), ("sample", 1, 5, 0), ("sample", 2, 9, 2), ("sample", 0, 1, 0), ("sample", 1, 0, 0)]
    run_scenario(["alpha", "beta", "gamma"], ops)
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        try:
            for run, seq in (("alpha", 3), ("beta", 5), ("gamma", 9)):
                env.service.ingest(raw_of(make_sample(run, seq)))
            firsts = {run: [s for s in env.sigs(run) if s[0] == "gap"][0][1] for run in ("alpha", "beta", "gamma")}
            assert firsts == {"alpha": "gap-0001", "beta": "gap-0001", "gamma": "gap-0001"}
            assert env.service.ingest(raw_of(make_sample("alpha", 3))).body["status"] == "duplicate"  # alpha's seq 3, not beta's
            assert env.service.ingest(raw_of(make_sample("beta", 3))).body["status"] == "reset"
        finally:
            env.close()


def test_rejections_with_and_without_a_valid_run_id_land_in_different_files() -> None:
    ops: list[tuple[Any, ...]] = [
        ("bad_valid", 0, "bad_ts"), ("bad_novalid", "bad_run"), ("bad_valid", 1, "no_pid"), ("bad_novalid", "huge"),
        ("bad_novalid", "bad_json"), ("restart",), ("bad_valid", 0, "extra"), ("sample", 0, 0, 0),
    ]
    run_scenario(["one", "two"], ops)


def test_reopening_a_run_after_restart_only_appends_resumed_metadata_to_that_run() -> None:
    ops: list[tuple[Any, ...]] = [("sample", 0, 0, 0), ("sample", 1, 0, 0), ("restart",), ("sample", 1, 1, 0), ("restart",), ("sample", 0, 2, 1)]
    run_scenario(["one", "two"], ops)


# --- negative controls -------------------------------------------------------------------------------------


def test_the_oracle_notices_a_record_in_the_wrong_file_and_a_wrong_gap_number() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        model = Model()
        try:
            for run in ("aa", "bb"):
                apply_op(env, model, ["aa", "bb"], ("sample", 0 if run == "aa" else 1, 4, 0))
            check_files(env, model)  # the real result passes
            a, b = env.path("aa"), env.path("bb")
            original_a = a.read_bytes()
            a.write_bytes(original_a + b.read_bytes().splitlines(keepends=True)[1])  # bb's sample line appended to aa
            with pytest.raises(AssertionError):
                check_files(env, model)
            a.write_bytes(original_a)
            check_files(env, model)
            real_next = model.runs["bb"].gap_next
            model.runs["bb"].gap_next = real_next + 3  # a shared or advanced gap counter
            with pytest.raises(AssertionError):
                check_state(env, model)
            model.runs["bb"].gap_next = real_next
            check_state(env, model)
            b.write_bytes(b.read_bytes().replace(b"gap-0001", b"gap-0002"))
            with pytest.raises(AssertionError):
                check_files(env, model)
        finally:
            env.close()


# --- concurrency (deterministic oracle over the real serialized order) ---------------------------------------


class Recorder:
    """Wraps `registry.append` (called by the service under the run lock) and logs appends in completion order."""

    def __init__(self, service: IngestService, *, hook: Any = None) -> None:
        self.log: list[tuple[str, int, int]] = []  # (run_id, seq, dropped) of sample and anomaly appends, in order
        self.inflight: dict[str, int] = {}
        self.max_inflight: dict[str, int] = {}
        self._guard = threading.Lock()
        self.hook = hook
        registry = service.registry
        original = registry.append

        def append(state: Any, records: Any) -> Any:
            run = state.run_id
            with self._guard:
                self.inflight[run] = self.inflight.get(run, 0) + 1
                self.max_inflight[run] = max(self.max_inflight.get(run, 0), self.inflight[run])
            try:
                if self.hook is not None:
                    self.hook(run, records)
                out = original(state, records)
                head = records[0]
                if head["record_type"] in ("sample", "sequence_anomaly"):
                    sample = head["sample"]
                    with self._guard:
                        self.log.append((run, sample["seq"], sample["dropped"]))
                return out
            finally:
                with self._guard:
                    self.inflight[run] -= 1

        registry.append = append  # type: ignore[method-assign]


def run_threads(targets: list[Any]) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(targets), timeout=10)

    def wrap(fn: Any) -> Any:
        def go() -> None:
            try:
                barrier.wait()
                fn()
            except BaseException as exc:  # reported to the test thread
                errors.append(exc)

        return go

    threads = [threading.Thread(target=wrap(fn), daemon=True) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive(), "a worker thread did not finish"
    return errors


def verify_serialized(env: Env, recorder: Recorder, expected_ingests: int, runs: list[str]) -> list[tuple[str, int, int]]:
    """Apply the model to the recorded order and compare every file and state; return the order."""
    assert len(recorder.log) == expected_ingests, "an acknowledged ingest has no record, or has two"
    assert all(v == 1 for v in recorder.max_inflight.values()), f"two appends to one run overlapped: {recorder.max_inflight}"
    model = Model()
    for run, seq, dropped in recorder.log:
        model.sample(run, seq, dropped)
    check_files(env, model, ordered=False)
    # Each request's stored recv_time and returned recv_time are the clock value it read on arrival, independent of
    # the order in which the appends completed.
    assert len(env.tracked) == expected_ingests
    for run, seq, arrival, result in env.tracked:
        assert result.recv_time == arrival and result.body["recv_time"] == arrival, (run, seq)
    stored: dict[str, list[tuple[str, int]]] = {}
    for run in runs:
        for rec in read_evidence(env.path(run)).records:
            if rec["record_type"] in ("sample", "sequence_anomaly"):
                stored.setdefault(rec["recv_time"], []).append((run, rec["sample"]["seq"]))
    assert {k: sorted(v) for k, v in stored.items()} == {a: [(r, s)] for r, s, a, _ in env.tracked}
    for run in runs:
        data = env.path(run).read_bytes()
        assert data.endswith(b"\n") and all(line.startswith(b"{") and line.endswith(b"}") for line in data.splitlines())
    for run in model.opened:
        m = model.runs[run]
        state = env.service.registry.get(run)
        assert state.highest_seq == m.highest and state.next_gap_number == m.gap_next
        assert {n for n in range(0, 40) if n in state.seen} == m.seen
    return recorder.log


def widen(run: str, records: Any) -> None:
    time.sleep(0.002)  # only widens the race window; correctness never depends on it


def test_two_threads_ingest_into_one_run() -> None:
    orders = set()
    for seed in range(20):
        rng = random.Random(seed)
        seqs = list(range(10)) + [rng.randrange(10) for _ in range(4)]  # new, duplicate and reset arrivals
        rng.shuffle(seqs)
        halves = [seqs[0::2], seqs[1::2]]
        drops = {i: rng.choice([0, 0, 0, 1, 3]) for i in range(len(seqs))}
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(Path(tmp))
            try:
                recorder = Recorder(env.service, hook=widen)
                results: list[Any] = []

                def worker(part: list[int], offset: int) -> Any:
                    def go() -> None:
                        for i, seq in enumerate(part):
                            results.append(env.ingest_tracked("same", seq, drops[(i * 2 + offset) % len(seqs)]))

                    return go

                errors = run_threads([worker(halves[0], 0), worker(halves[1], 1)])
                assert errors == []
                assert len(results) == len(seqs) and all(r.http_status == 200 for r in results)
                orders.add(tuple(s for _, s, _ in verify_serialized(env, recorder, len(seqs), ["same"])))
            finally:
                env.close()
    assert len(orders) > 5, "the serialized orders hardly varied; the test is not exercising interleavings"


def test_several_threads_ingest_into_different_runs_without_sharing_a_lock() -> None:
    runs = ["w0", "w1", "w2", "w3"]
    for round_number in range(5):
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(Path(tmp))
            try:
                rendezvous = threading.Barrier(len(runs), timeout=10)
                first_seen: set[str] = set()

                def hook(run: str, records: Any) -> None:
                    if run not in first_seen:  # every run's first append waits for all runs: a global lock would deadlock it
                        first_seen.add(run)
                        rendezvous.wait()

                recorder = Recorder(env.service, hook=hook)

                def worker(run: str, k: int) -> Any:
                    def go() -> None:
                        seqs = [3 + k, 0, 4 + k, 1, 2, 3 + k, 9, 8]
                        for i, seq in enumerate(seqs):
                            assert env.ingest_tracked(run, seq, i % 3 if i > 3 else 0).http_status == 200

                    return go

                errors = run_threads([worker(run, k) for k, run in enumerate(runs)])
                assert errors == [], errors
                verify_serialized(env, recorder, 8 * len(runs), runs)
                assert sorted(recorder.max_inflight) == runs
                assert all([s for s in env.sigs(run) if s[0] == "gap"][0][1] == "gap-0001" for run in runs)
            finally:
                env.close()


def test_concurrent_requests_with_seq_numbers_arriving_out_of_order() -> None:
    seen_orders: set[tuple[int, ...]] = set()
    for seed in range(15):
        rng = random.Random(100 + seed)
        seqs = list(range(8))
        rng.shuffle(seqs)
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(Path(tmp))
            try:
                recorder = Recorder(env.service, hook=widen)
                errors = run_threads([(lambda s=s: env.ingest_tracked("ooo", s)) for s in seqs])
                assert errors == []
                seen_orders.add(tuple(x for _, x, _ in verify_serialized(env, recorder, len(seqs), ["ooo"])))
            finally:
                env.close()
    assert len(seen_orders) > 3


def test_a_request_that_arrives_while_another_is_appending_waits_and_sees_its_result() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        try:
            inside, release, y_started = threading.Event(), threading.Event(), threading.Event()

            def hook(run: str, records: Any) -> None:
                if records[0]["record_type"] == "sample" and records[0]["sample"]["seq"] == 5:
                    inside.set()
                    assert release.wait(10)

            recorder = Recorder(env.service, hook=hook)
            out: dict[str, Any] = {}
            x = threading.Thread(target=lambda: out.update(x=env.ingest_tracked("lk", 5)))
            x.start()
            assert inside.wait(10)

            def late() -> None:
                y_started.set()
                out["y"] = env.ingest_tracked("lk", 3)

            y = threading.Thread(target=late)
            y.start()
            assert y_started.wait(10)
            time.sleep(0.05)  # lets a missing lock show up; with the lock this changes nothing
            assert "y" not in out, "the second request finished while the first still held the run"
            release.set()
            x.join(10)
            y.join(10)
            assert not x.is_alive() and not y.is_alive()
            assert out["x"].body["status"] == "stored" and out["y"].body["status"] == "reset"
            assert [s for _, s, _ in recorder.log] == [5, 3]
            verify_serialized(env, recorder, 2, ["lk"])
        finally:
            env.close()
