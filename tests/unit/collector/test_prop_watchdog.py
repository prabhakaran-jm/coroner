"""Task 3.10, Property 14: the silence watchdog emits one gap per silence period (Requirements 6.2, 6.13; D29 to D33).

Design, Property 14 (verbatim): "For any schedule of Sample arrivals, watchdog ticks, and an optional termination
under an injected clock, the watchdog appends exactly one `silence_timeout` gap per continuous silence period longer
than 3 times the Sample_Interval, detects each within one Sample_Interval after the threshold elapses, appends none
after a termination is recorded, appends none while Samples keep arriving, and never changes Missing_Sample_Count."

**Validates: Requirements 6.2, 6.13**

Real IngestService and SilenceWatchdog (`create_watchdog(service)`, `check_once()`), real temp evidence files, an
injected monotonic clock and a separate injected UTC clock. Nothing sleeps. The oracle (`Model`) is independent of the
production classification and watchdog code: it keeps its own sequence bookkeeping and compares elapsed time with the specified
float semantics (`float_silent`: elapsed_ms = (now - last) * 1000.0 as doubles, silence iff elapsed_ms > 3 * interval_ms,
strict, no tolerance). An exact-rational reference (`rational_silent`, Fraction) is used only for boundary analysis.
"""
from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import math
import random

from coroner.collector.config import CollectorConfig
from coroner.collector.service import create_ingest_service
from coroner.collector.watchdog import create_watchdog
from coroner.common.timeutil import format_utc_ms
from coroner.evidence import writer as writer_module
from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.schema import validate_record
from coroner.evidence.writer import Durability
from tests.unit.collector.test_watchdog import BASE, TERMINATION, Env, FakeProbe, Mono, sample

RUNS = ("r1", "r2")
SETTINGS = settings(
    max_examples=300, deadline=None, derandomize=True, database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much, HealthCheck.data_too_large],
)


# --- injected clocks and storage faults ------------------------------------------------------------------


class Wall:
    """The UTC clock: follows the monotonic clock by default, distinct on every call, with a settable jump."""

    def __init__(self, mono: Mono) -> None:
        self.mono, self.offset_ms, self.frozen, self.n = mono, 0, False, 0

    def now(self) -> str:
        if self.frozen:
            return format_utc_ms(BASE + timedelta(milliseconds=self.offset_ms))
        self.n += 1
        return format_utc_ms(BASE + timedelta(milliseconds=round(self.mono.t * 1000) + self.offset_ms + self.n))


class PEnv(Env):
    """test_watchdog.Env with an injectable wall clock and fsync / directory-fsync failures."""

    def __init__(self, tmp: Path, wall: Wall | None = None, tick_ms: float | None = None) -> None:
        self.directory = tmp / "evidence"
        self.config = CollectorConfig(Durability.CRASH, 65536, 50, self.directory)
        self.mono = Mono()
        self.wall = wall if wall is not None else Wall(self.mono)
        self.probe = FakeProbe()
        self.fail_fsync = 0
        self.fail_dir = 0
        self.tick_ms = tick_ms
        self.build()

    def _fsync(self, fd: int) -> None:
        if self.fail_fsync:
            self.fail_fsync -= 1
            raise OSError("fsync failed")

    def _fsync_dir(self, path: Path) -> None:
        if self.fail_dir:
            self.fail_dir -= 1
            raise OSError("directory fsync failed")

    def build(self) -> None:
        self.service = create_ingest_service(
            self.config, fsync=self._fsync, fsync_dir=self._fsync_dir, now=self.wall.now, monotonic=self.mono,
            storage_probe=self.probe,
        )
        self.service.registry.start()
        self.wd = create_watchdog(self.service, tick_ms=self.tick_ms)


class _Bad:
    def __init__(self, real: Any, mode: str) -> None:
        self._real, self._mode = real, mode

    def __enter__(self) -> "_Bad":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        if self._mode == "write":
            raise OSError("disk full")
        if self._mode == "partial":
            self._real.write(data[:30])
            raise OSError("disk full")
        if self._mode == "full":
            self._real.write(data)
            raise OSError("late error")
        return self._real.write(data)

    def flush(self) -> None:
        if self._mode == "flush":
            raise OSError("flush failed")
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()


LANDED = {"full", "flush", "fsync"}  # the whole line reached the file before the error


class Inject:
    """A one-shot failure for the next append to one file. Install with `mock.patch.object(writer_module, "open", ...)`."""

    def __init__(self) -> None:
        self.mode: str | None = None
        self.name = ""

    def arm(self, mode: str, run_id: str) -> None:
        self.mode, self.name = mode, f"{run_id}.jsonl"

    def disarm(self) -> None:
        self.mode = None

    def __call__(self, path: Any, mode: str) -> Any:
        real = open(path, mode)
        if self.mode is None or Path(path).name != self.name:
            return real
        now, self.mode = self.mode, None
        return _Bad(real, now)


# --- the independent oracle ------------------------------------------------------------------------------


def float_silent(now_s: float, last_s: float, interval_ms: int) -> bool:
    """The specified observable semantics of "silence longer than 3 times the Sample_Interval".

    Both clock readings are monotonic seconds held as IEEE-754 doubles (Python floats). Elapsed time in milliseconds
    is the double `(now_s - last_s) * 1000.0`: the subtraction is rounded to the nearest double, then the product is
    rounded to the nearest double. The threshold is the integer `3 * interval_ms` (exact), compared with Python's
    float/int comparison, which is exact. Silence is the strict comparison `elapsed_ms > threshold`: elapsed equal to
    the threshold is not silence. No tolerance of any kind. This is written from the specification, independently of
    the watchdog; the watchdog is never imported or called here for an expectation.
    """
    elapsed_ms = (now_s - last_s) * 1000.0
    return elapsed_ms > 3 * interval_ms


def rational_silent(now_s: float, last_s: float, interval_ms: int) -> bool:
    """Exact-rational reference (reporting and boundary analysis only; it never decides the main property):
    `(Fraction(now) - Fraction(last)) * 1000 > 3 * interval` over the exact values of the two doubles."""
    return (Fraction(now_s) - Fraction(last_s)) * 1000 > 3 * interval_ms


class Model:
    """What one run's silence history must be. Own sequence bookkeeping; no production classification calls."""

    def __init__(self) -> None:
        self.highest: int | None = None
        self.seen: set[int] = set()
        self.missing: set[int] = set()
        self.last_s: float | None = None
        self.last_ms = 0
        self.interval = 0
        self.open = False
        self.terminated = False
        self.monitored = False
        self.silences = 0
        self.dirty = False
        self.partial = False

    def classify(self, seq: int) -> str:
        if seq in self.seen:
            return "duplicate"
        if self.highest is not None and seq < self.highest:
            return "reset"
        return "new"

    def ingest(self, seq: int, interval: int, ms: int) -> str:
        kind = self.classify(seq)
        if kind == "new":
            expected = 0 if self.highest is None else self.highest + 1
            self.missing.update(range(expected, seq))
            self.highest = seq
            self.seen.add(seq)
            self.last_s, self.last_ms, self.interval = ms / 1000.0, ms, interval
            self.open = False
            self.monitored = True
        return kind

    def eligible(self) -> bool:
        return self.monitored and not self.terminated and not self.open and self.last_s is not None

    def due(self, now_s: float) -> str:
        """"yes" or "no": one deterministic expected outcome, decided by `float_silent` (the float semantics
        specified above) for an eligible run (monitored, not terminated, no silence open)."""
        if not self.eligible():
            return "no"
        assert self.last_s is not None
        return "yes" if float_silent(now_s, self.last_s, self.interval) else "no"


class ExtraGapModel(Model):
    """Negative control: the oracle also flags the exact threshold (>=, same float semantics)."""

    def due(self, now_s: float) -> str:
        if not self.eligible():
            return "no"
        assert self.last_s is not None
        return "yes" if (now_s - self.last_s) * 1000.0 >= 3 * self.interval else "no"


class MissingGapModel(Model):
    """Negative control: the oracle never expects a gap."""

    def due(self, now_s: float) -> str:
        return "no"


class Runner:
    def __init__(self, env: PEnv, inject: Inject, model_cls: type[Model] = Model) -> None:
        self.env, self.inject = env, inject
        self.models = {r: model_cls() for r in RUNS}
        self.ms = 0
        self.returned: dict[str, list[str]] = {r: [] for r in RUNS}

    def set_time(self, ms: int) -> None:
        self.ms = ms
        self.env.mono.t = ms / 1000.0

    def pick(self, run: str, kind: str, k: int) -> int:
        m = self.models[run]
        if kind == "next":
            return 0 if m.highest is None else m.highest + 1
        if kind == "gap":
            return k if m.highest is None else m.highest + 1 + k
        if kind == "reset" and m.highest is not None:
            free = [s for s in range(m.highest) if s not in m.seen]
            if free:
                return free[k % len(free)]
        return sorted(m.seen)[k % len(m.seen)] if m.seen else 0

    def tick(self, fail: tuple[str, str] | None = None) -> None:
        now_s = self.env.mono.t
        verdict = {r: self.models[r].due(now_s) for r in RUNS}
        failing = None
        if fail is not None and verdict[fail[0]] == "yes" and not self.models[fail[0]].dirty:
            failing = fail
            self.inject.arm(fail[1], fail[0])
        try:
            got = self.env.wd.check_once()
        finally:
            self.inject.disarm()
        by_run = [g.split(":")[0] for g in got]
        assert len(by_run) == len(set(by_run)), got
        for r in RUNS:
            if failing is not None and r == failing[0]:
                assert r not in by_run, (now_s, got)  # no successful write is claimed
            elif verdict[r] == "yes":
                assert r in by_run, (now_s, got, verdict)
            else:
                assert verdict[r] == "no" and r not in by_run, (now_s, got, verdict)
        for g in got:
            run, gap_id = g.split(":")
            self.returned[run].append(gap_id)
            self.models[run].silences += 1
            self.models[run].open = True
            self.models[run].dirty = False
        if failing is not None:
            m = self.models[failing[0]]
            m.dirty = True
            m.partial = m.partial or failing[1] == "partial"
            if failing[1] in LANDED:
                m.silences += 1
                m.open = True
            state = self.env.state(failing[0])
            assert state.dirty and not state.silence_open
            assert self.env.probe.invalidations >= 1

    def apply(self, ev: tuple[Any, ...]) -> None:
        kind = ev[0]
        if kind == "adv":
            self.set_time(self.ms + ev[1])
        elif kind == "wall":
            self.env.wall.offset_ms += ev[1]
        elif kind == "tick":
            self.tick()
        elif kind == "th_tick":  # land at last + 3 * interval + offset of one run, then tick
            m = self.models[ev[1]]
            if m.last_s is not None and m.last_ms + 3 * m.interval + ev[2] >= self.ms:
                self.set_time(m.last_ms + 3 * m.interval + ev[2])
            self.tick()
        elif kind == "fail_tick":
            self.tick(fail=(ev[1], ev[2]))
        elif kind == "ingest":
            _, run, how, k, interval = ev
            seq = self.pick(run, how, k)
            m = self.models[run]
            result = self.env.ingest(run, seq, interval)
            assert result.http_status == 200, result
            m.ingest(seq, interval, self.ms)
            m.dirty = False
        elif kind == "term":
            m = self.models[ev[1]]
            if m.seen and not m.terminated:
                result = self.env.service.record_termination(ev[1], json.dumps(TERMINATION).encode())
                assert result.http_status == 200, result
                m.terminated = True
                m.dirty = False
        elif kind == "restart":
            self.env.restart()
            for m in self.models.values():
                m.monitored = False
                m.dirty = False

    def check_files(self) -> None:
        env = self.env
        for run, m in self.models.items():
            if not env.path(run).exists():
                assert not m.seen
                continue
            result = read_evidence(env.path(run))
            for rec in result.records:
                assert validate_record(rec) == [], rec
            if not m.partial:
                assert result.errors == [], result.errors
            silences = [r for r in result.records if r["record_type"] == "gap" and r["gap_type"] == "silence_timeout"]
            assert len(silences) == m.silences, (run, len(silences), m.silences)
            ids = [r["gap_id"] for r in result.records if r["record_type"] == "gap"]
            assert len(ids) == len(set(ids)), ids
            assert [s["gap_id"] for s in silences] == [g for g in self.returned[run] if g in {s["gap_id"] for s in silences}]
            for s in silences:
                assert s["sample_count"] == "unknown" and not {"first_missing_seq", "last_missing_seq", "missing_count"} & set(s)
            if m.terminated:
                tail = [r["record_type"] if r["record_type"] != "gap" else r["gap_type"] for r in result.records]
                after = tail[tail.index("termination") + 1:]
                assert "silence_timeout" not in after
            if m.seen:
                assert summarize(result).missing_sample_count == len(m.missing)


# --- Property 14: generated schedules --------------------------------------------------------------------

intervals = st.one_of(st.sampled_from([100, 250, 500, 1000, 60000]), st.integers(100, 60000))
run_ids = st.sampled_from(RUNS)
events = st.one_of(
    st.tuples(st.just("adv"), st.one_of(st.integers(0, 3), st.sampled_from([50, 99, 100, 299, 300, 301, 1499, 1500, 1501, 5000]), st.integers(0, 200000))),
    st.tuples(st.just("tick")),
    st.tuples(st.just("tick")),
    st.tuples(st.just("th_tick"), run_ids, st.sampled_from([-1, 0, 1])),
    st.tuples(st.just("ingest"), run_ids, st.sampled_from(["next", "next", "gap", "dup", "reset"]), st.integers(1, 5), intervals),
    st.tuples(st.just("term"), run_ids),
    st.tuples(st.just("restart")),
    st.tuples(st.just("wall"), st.integers(-100000, 100000)),
    st.tuples(st.just("fail_tick"), run_ids, st.sampled_from(["write", "partial", "full", "flush"])),
)


def run_schedule(schedule: list[tuple[Any, ...]], tick_ms: int, model_cls: type[Model] = Model) -> Runner:
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(writer_module, "open", Inject(), create=True) as patched:
        inject = patched
        env = PEnv(Path(tmp), tick_ms=tick_ms)
        runner = Runner(env, inject, model_cls)
        try:
            for ev in schedule:
                runner.apply(ev)
            runner.apply(("tick",))
            runner.check_files()
        finally:
            env.service.registry.close()
        return runner


@SETTINGS
@given(st.lists(events, max_size=25), st.integers(1, 99))
def test_property_14_schedules(schedule: list[tuple[Any, ...]], tick_ms: int) -> None:
    run_schedule(schedule, tick_ms)


# The oracle is exercised by negative controls: a wrong oracle must be caught on a fixed schedule.
BOUNDARY_SCHEDULE: list[tuple[Any, ...]] = [("ingest", "r1", "next", 1, 500), ("th_tick", "r1", 0), ("th_tick", "r1", 1)]


def test_the_oracle_agrees_on_the_fixed_boundary_schedule() -> None:
    runner = run_schedule(BOUNDARY_SCHEDULE, 10)
    assert runner.models["r1"].silences == 1


def test_negative_control_an_oracle_that_flags_the_exact_threshold_is_caught() -> None:
    with pytest.raises(AssertionError):
        run_schedule(BOUNDARY_SCHEDULE, 10, ExtraGapModel)


def test_negative_control_an_oracle_that_expects_no_gap_is_caught() -> None:
    with pytest.raises(AssertionError):
        run_schedule(BOUNDARY_SCHEDULE, 10, MissingGapModel)


# --- detection timing -------------------------------------------------------------------------------------


@SETTINGS
@given(intervals, st.integers(1, 99), st.integers(0, 98), st.integers(0, 10**6))
def test_detection_lag_is_within_one_interval_with_regular_ticks(interval: int, tick_ms: int, lead: int, last_ms: int) -> None:
    """Regular ticks `tick_ms` apart through the threshold: no gap at or before it, one gap on the first tick after."""
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=tick_ms)
        env.mono.t = last_ms / 1000.0
        assert env.ingest(interval_ms=interval).http_status == 200
        threshold = last_ms + 3 * interval
        t = max(last_ms, threshold - lead)  # the regular ticks begin shortly before the threshold
        detected = None
        while detected is None:
            env.mono.t = t / 1000.0
            got = env.wd.check_once()
            if got:
                detected = t
            else:
                t += tick_ms
        # Never early by a millisecond; late by at most one tick, which is less than one Sample interval.
        assert threshold <= detected <= threshold + tick_ms < threshold + interval
        assert len(env.silences()) == 1
        for _ in range(5):  # continuing silence adds nothing
            t += tick_ms
            env.mono.t = t / 1000.0
            assert env.wd.check_once() == []
        assert len(env.silences()) == 1


def test_lag_example_with_the_smallest_interval_and_the_largest_tick() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=99)
        env.ingest(interval_ms=100)
        times = list(range(150, 520, 99))  # ticks 99 ms apart; the threshold is 300 ms
        found = []
        for t in times:
            env.mono.t = t / 1000.0
            if env.wd.check_once():
                found.append(t)
        assert found == [348] and 348 - 300 < 100


def test_samples_that_keep_arriving_never_make_a_gap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=1)
        for seq in range(30):
            env.mono.t = seq * 0.5  # one Sample per interval, ticks in between
            env.ingest(seq=seq, interval_ms=500)
            for off in (0.0, 0.25, 0.4999):
                env.mono.t = seq * 0.5 + off
                assert env.wd.check_once() == []
        env.mono.t = 29 * 0.5 + 1.5  # exactly 3 * interval after the last: still not silence
        assert env.wd.check_once() == []
        assert env.silences() == []


# --- floating-point boundaries ----------------------------------------------------------------------------


def _impl_silent(now: float, last: float, interval: int) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.mono.t = last
        assert env.ingest(interval_ms=interval).http_status == 200
        env.mono.t = now
        return env.wd.check_once() == ["r1:gap-0001"]


def t_exact(last: float, interval: int) -> float:
    """The float nearest to the mathematical threshold `last + 3 * interval / 1000`: the sum is computed exactly in
    rationals (`Fraction(last) + Fraction(3 * interval, 1000)`) and converted with `float()`, which rounds to nearest."""
    return float(Fraction(last) + Fraction(3 * interval, 1000))


def probes_around(last: float, interval: int, below: int, above: int) -> dict[int, float]:
    """{k: the float k representable steps from t_exact}: negative k below it, positive above (math.nextafter)."""
    centre = t_exact(last, interval)
    out = {0: centre}
    lo = hi = centre
    for k in range(1, max(below, above) + 1):
        lo, hi = math.nextafter(lo, -math.inf), math.nextafter(hi, math.inf)
        if k <= below:
            out[-k] = lo
        if k <= above:
            out[k] = hi
    return dict(sorted(out.items()))


FP_LASTS = [0.3, 0.0, 12345.678]
FP_INTERVALS = [500, 60000]

# (last, interval) -> {k: (probe repr, float verdict, rational verdict)}. Exact values, no tolerance. Only
# last = 0.3 has a case where the two verdicts differ: the stored double 0.3 is below 0.3, so the exact elapsed
# time at t_exact is already above the threshold (rational: silent) while the double arithmetic gives
# (1.8 - 0.3) * 1000.0 == 1500.0 (float: not silent). The direction is LATE: one float step, at k = 0.
FP_TABLE: dict[tuple[float, int], dict[int, tuple[str, bool, bool]]] = {
    (0.3, 500): {-1: ("1.7999999999999998", False, False), 0: ("1.8", False, True), 1: ("1.8000000000000003", True, True)},
    (0.3, 60000): {-1: ("180.29999999999998", False, False), 0: ("180.3", False, True), 1: ("180.30000000000004", True, True)},
    (0.0, 500): {-1: ("1.4999999999999998", False, False), 0: ("1.5", False, False), 1: ("1.5000000000000002", True, True)},
    (0.0, 60000): {-1: ("179.99999999999997", False, False), 0: ("180.0", False, False), 1: ("180.00000000000003", True, True)},
    (12345.678, 500): {-1: ("12347.177999999998", False, False), 0: ("12347.178", False, False), 1: ("12347.178000000002", True, True)},
    (12345.678, 60000): {-1: ("12525.677999999998", False, False), 0: ("12525.678", False, False), 1: ("12525.678000000002", True, True)},
}


@pytest.mark.parametrize(("last", "interval"), [(la, iv) for la in FP_LASTS for iv in FP_INTERVALS])
def test_fixed_boundary_probes_exact_values_oracle_and_direction(last: float, interval: int) -> None:
    """At t_exact and one float step either side: the real watchdog equals the float-semantics oracle (exactly),
    the probe values and both verdicts are the recorded constants, and the implementation is never early against the
    exact-rational verdict. Where the two verdicts differ the direction is recorded (late only)."""
    expected = FP_TABLE[(last, interval)]
    probes = probes_around(last, interval, 1, 1)
    differing = []
    for k, now in probes.items():
        impl = _impl_silent(now, last, interval)
        oracle, rational = float_silent(now, last, interval), rational_silent(now, last, interval)
        assert (repr(now), oracle, rational) == expected[k], (k, repr(now), oracle, rational)
        assert impl == oracle, (k, now, impl, oracle)  # the implementation equals the float-semantics oracle
        assert not (impl and not rational), (k, now)  # never detected where the exact verdict is "not silent"
        if oracle != rational:
            differing.append((k, "late" if rational and not oracle else "early"))
    assert differing == ([(0, "late")] if last == 0.3 else [])


def test_the_exact_threshold_point_is_not_silence_where_both_verdicts_are_not_silent() -> None:
    """Zero exact margin (last = 0.0 and last = 12345.678 at t_exact) is not silence; `>=` would be caught here."""
    for last in (0.0, 12345.678):
        for interval in FP_INTERVALS:
            now = t_exact(last, interval)
            assert not rational_silent(now, last, interval) and not float_silent(now, last, interval)
            assert not _impl_silent(now, last, interval)
            assert _impl_silent(math.nextafter(now, math.inf), last, interval)


def test_documented_one_float_step_late_detection_at_last_0_3() -> None:
    """Document: with last = 0.3 the first float strictly above the ideal rational threshold is t_exact itself, but
    the watchdog first detects one float step later (k = 1). Float steps late = 1 for both intervals."""
    for interval, first_rational in ((500, "1.8"), (60000, "180.3")):
        probes = probes_around(0.3, interval, 3, 3)
        rational_first = min(k for k, t in probes.items() if rational_silent(t, 0.3, interval))
        impl_first = min(k for k, t in probes.items() if _impl_silent(t, 0.3, interval))
        assert repr(probes[rational_first]) == first_rational
        assert (rational_first, impl_first) == (0, 1)
        assert impl_first - rational_first == 1  # one float step late (about 2.2e-16 s at 1.8 s, 2.8e-14 s at 180.3 s)


def _sweep_cases() -> list[tuple[float, int]]:
    rng = random.Random(14)
    cases = []
    for n in range(200):
        interval = rng.choice([100, 250, 500, 1000, 60000]) if n % 2 else rng.randint(100, 60000)
        if n % 3 == 0:
            last = rng.randint(0, 10**8) / 1000.0  # millisecond-valued, like the property schedules
        elif n % 3 == 1:
            last = rng.uniform(0.0, 1e5)  # arbitrary doubles
        else:
            last = round(rng.uniform(0.0, 1e4), 3)
        cases.append((last, interval))
    for n in range(1, 101):  # decimal-tenth readings (0.1 .. 10.0), where binary rounding is most visible
        cases.append((n / 10.0, 500 if n % 2 else 60000))
    return cases


def test_sweep_no_early_detection_and_at_most_one_float_step_late() -> None:
    """300 (last, interval) combinations (200 seeded mixed + 100 decimal tenths) x steps -1, 0, +1, +2 around t_exact. For every probe the implementation
    equals the float oracle and is never detected where the exact-rational verdict is "not silent". Lateness is
    measured over steps -3..+4: first detecting step minus first rational-silent step. Measured: 22 of 300 combinations are one
    step late, none early, maximum 1."""
    max_late, differing = 0, 0
    for last, interval in _sweep_cases():
        results = {}
        for k, now in probes_around(last, interval, 3, 4).items():
            impl = _impl_silent(now, last, interval)
            assert impl == float_silent(now, last, interval), (last, interval, k)
            if k in (-1, 0, 1, 2):
                assert not (impl and not rational_silent(now, last, interval)), ("early detection", last, interval, k)
            results[k] = (impl, rational_silent(now, last, interval))
        impl_first = min(k for k, (i, _) in results.items() if i)
        rational_first = min(k for k, (_, r) in results.items() if r)
        assert impl_first >= rational_first, ("early detection", last, interval)
        max_late = max(max_late, impl_first - rational_first)
        differing += impl_first != rational_first
    assert max_late <= 1, (max_late, differing)


def test_detection_deadline_at_the_boundary_is_within_one_sample_interval() -> None:
    """Property 14 deadline at the float boundary: the first detecting float is at most one float step after the
    threshold, far less than one Sample_Interval, for the documented hard case."""
    for interval in FP_INTERVALS:
        probes = probes_around(0.3, interval, 0, 1)
        detect = [t for t in probes.values() if _impl_silent(t, 0.3, interval)]
        assert detect and (min(detect) - 0.3) * 1000.0 - 3 * interval < interval


# --- failure and retry ------------------------------------------------------------------------------------


def _gap_ids(env: PEnv, run: str = "r1") -> list[str]:
    return [g["gap_id"] for g in env.records(run) if g["record_type"] == "gap"]


def _setup_two(tmp: Path) -> PEnv:
    env = PEnv(Path(tmp), tick_ms=50)
    env.ingest("r1")
    env.ingest("r2")
    env.mono.t = 2.0
    return env


@pytest.mark.parametrize("mode", ["write", "partial", "full", "flush"])
def test_append_failures_leave_state_clean_and_retry_through_open_run(mode: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = _setup_two(Path(tmp))
        inject = Inject()
        with mock.patch.object(writer_module, "open", inject, create=True):
            inject.arm(mode, "r1")
            assert env.wd.check_once() == ["r2:gap-0001"]  # r1 failed, r2 served in the same pass
        state = env.state("r1")
        assert state.silence_open is False and state.dirty is True
        assert env.probe.invalidations == 1
        valid_before = [r for r in env.records("r1") if r["record_type"] == "sample"]
        assert len(valid_before) == 1  # valid existing evidence is still readable
        # the retry
        again = env.wd.check_once()
        landed = mode in LANDED
        assert again == ([] if landed else ["r1:gap-0001"])
        silences = env.silences("r1")
        assert len(silences) == 1 and validate_record(silences[0]) == []
        assert env.state("r1").silence_open is True and env.state("r1").dirty is False
        assert env.wd.check_once() == []
        ids = _gap_ids(env)
        assert len(ids) == len(set(ids))
        if mode == "partial":
            repairs = [r for r in env.records("r1") if r["record_type"] == "repair"]
            assert len(repairs) == 1
        assert read_evidence(env.path("r1")).records  # still readable
        assert env.path("r2").exists() and len(env.silences("r2")) == 1
        env.service.registry.close()


def test_file_fsync_failure_after_the_line_landed_is_not_duplicated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = _setup_two(Path(tmp))
        env.fail_fsync = 1
        got = env.wd.check_once()  # r1 is visited first and its file fsync fails; r2 is still served
        assert got == ["r2:gap-0001"]
        state = env.state("r1")
        assert state.dirty and not state.silence_open and env.probe.invalidations == 1
        assert env.wd.check_once() == []  # recovery rebuilds silence_open from the file: nothing duplicated
        assert len(env.silences("r1")) == 1 and env.state("r1").silence_open
        env.service.registry.close()


def test_directory_fsync_failure_during_recovery_then_one_gap() -> None:
    """Run files already exist when the watchdog writes, so the creation-time directory fsync is not reachable from
    the watchdog append. The required directory fsync that is reachable is the one in open_run recovery."""
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.ingest("r1")
        env.mono.t = 2.0
        inject = Inject()
        with mock.patch.object(writer_module, "open", inject, create=True):
            inject.arm("partial", "r1")
            assert env.wd.check_once() == []
        env.fail_dir = 1
        assert env.wd.check_once() == []  # recovery fails at the directory fsync: nothing written
        assert env.state().dirty and not env.state().silence_open and env.probe.invalidations == 2
        assert env.silences() == []
        assert env.wd.check_once() == ["r1:gap-0001"]
        assert len(env.silences()) == 1
        repairs = [r for r in env.records() if r["record_type"] == "repair"]
        assert len(repairs) == 1
        env.service.registry.close()


def test_file_fsync_failure_during_recovery_then_one_gap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.ingest("r1")
        env.mono.t = 2.0
        inject = Inject()
        with mock.patch.object(writer_module, "open", inject, create=True):
            inject.arm("write", "r1")
            assert env.wd.check_once() == []
        env.fail_fsync = 1
        assert env.wd.check_once() == []
        assert env.state().dirty and env.probe.invalidations == 2 and env.silences() == []
        assert env.wd.check_once() == ["r1:gap-0001"]
        assert [g["gap_id"] for g in env.silences()] == ["gap-0001"]
        env.service.registry.close()


def test_a_fragment_is_quarantined_and_the_fresh_gap_has_a_non_reused_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.ingest("r1")
        env.mono.t = 2.0
        inject = Inject()
        with mock.patch.object(writer_module, "open", inject, create=True):
            inject.arm("partial", "r1")
            env.wd.check_once()
        raw = env.path().read_bytes()
        assert not raw.endswith(b"\n")  # an incomplete gap line
        assert env.wd.check_once() == ["r1:gap-0001"]
        result = read_evidence(env.path())
        repairs = [r for r in result.records if r["record_type"] == "repair"]
        assert len(repairs) == 1
        gap_ids = [r["gap_id"] for r in result.records if r["record_type"] == "gap"]
        assert gap_ids == ["gap-0001"]  # the fragment never became a gap record, so no id is reused by a valid record
        for r in result.records:
            assert validate_record(r) == []


# --- clock behaviour --------------------------------------------------------------------------------------


@pytest.mark.parametrize("jump_ms", [-3_600_000, -1, 0, 1, 3_600_000])
def test_wall_clock_jumps_do_not_change_detection_or_validity(jump_ms: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.ingest()
        first = env.state().last_sample_recv_time
        env.wall.offset_ms += jump_ms  # the UTC clock jumps; the monotonic clock does not
        env.mono.t = 1.5
        assert env.wd.check_once() == []  # exactly 3 * interval on the monotonic clock
        env.mono.t = 1.501
        assert env.wd.check_once() == ["r1:gap-0001"]
        gap = env.silences()[0]
        assert validate_record(gap) == []
        assert gap["start_time"] == first  # start_time may come after end_time under a backward jump; D31 allows it
        assert read_evidence(env.path()).errors == []


def test_d31_does_not_require_start_time_not_after_end_time() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.wall.offset_ms = 10_000_000
        env.ingest()
        env.wall.offset_ms = -10_000_000  # a large backward jump
        env.mono.t = 2.0
        assert env.wd.check_once() == ["r1:gap-0001"]
        gap = env.silences()[0]
        assert gap["start_time"] > gap["end_time"] and validate_record(gap) == []


def test_frozen_wall_clock_gives_identical_timestamps_and_still_one_gap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.wall.frozen = True
        env.ingest(seq=0)
        env.mono.t = 2.0
        assert env.wd.check_once() == ["r1:gap-0001"]
        gap = env.silences()[0]
        assert gap["start_time"] == gap["end_time"] == gap["recv_time"]  # repeated identical UTC timestamps
        for t in (3.0, 4.0):
            env.mono.t = t
            assert env.wd.check_once() == []
        assert len(env.silences()) == 1


def test_wall_time_does_not_drive_detection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.ingest()
        env.wall.offset_ms += 10_000_000  # an hour-scale forward jump with no monotonic progress
        assert env.wd.check_once() == []
        assert env.silences() == []
        sample_ts = json.loads(sample("r1", 0))["ts"]
        assert env.state().last_sample_recv_time != sample_ts  # recv_time and the Sample's own ts are distinct


def test_the_locked_recheck_sees_a_sample_accepted_while_waiting_for_the_lock() -> None:
    import threading

    from coroner.collector.state import NewSample, Outcome

    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.ingest()
        env.mono.t = 2.0  # past the threshold: the unlocked pre-check selects the run
        state = env.state()
        results: list[list[str]] = []
        state.lock.acquire()
        thread = threading.Thread(target=lambda: results.append(env.wd.check_once()))
        thread.start()
        try:
            state.apply(Outcome(new_sample=NewSample(1, "2025-01-01T12:00:09.000Z", 500, 2.0)))  # accepted meanwhile
        finally:
            state.lock.release()
        thread.join(10)
        assert not thread.is_alive() and results == [[]]
        assert env.silences() == []


# --- repeated silence, termination, restart ---------------------------------------------------------------


def test_repeated_silence_periods_each_get_exactly_one_gap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        t = 0.0
        for seq in range(4):
            env.mono.t = t
            env.ingest(seq=seq * 2, interval_ms=500)  # each Sample also opens a discontinuity gap
            for step in range(1, 12):
                env.mono.t = t + step * 0.25
                env.wd.check_once()
            t += 10.0
        silences = env.silences()
        assert len(silences) == 4
        ids = _gap_ids(env)
        assert len(ids) == len(set(ids)) == 7  # 3 discontinuities + 4 silences
        assert summarize(read_evidence(env.path())).missing_sample_count == 3


def test_termination_stops_silence_and_a_late_sample_does_not_revive_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.ingest()
        env.service.record_termination("r1", json.dumps(TERMINATION).encode())
        env.mono.t = 50.0
        assert env.wd.check_once() == []
        env.ingest(seq=1)
        env.mono.t = 100.0
        assert env.wd.check_once() == [] and env.silences() == []


def test_restart_does_not_reflag_history_until_a_new_accepted_sample() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = PEnv(Path(tmp), tick_ms=50)
        env.ingest(seq=0)
        env.mono.t = 2.0
        env.wd.check_once()
        env.restart()
        env.mono.t = 500.0
        assert env.wd.check_once() == []
        env.ingest(seq=0)  # a duplicate does not start monitoring
        assert env.wd.check_once() == []
        env.ingest(seq=1)  # accepted: monitoring starts, the old silence is closed
        env.mono.t = 502.0
        assert env.wd.check_once() == ["r1:gap-0002"]
        assert len(env.silences()) == 2
