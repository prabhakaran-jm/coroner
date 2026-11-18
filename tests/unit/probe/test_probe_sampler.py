"""Task 4.2: Sample construction, request count and the absolute-deadline sampler (Requirements 2.1, 2.2, 2.4, 2.5, 2.7, 2.9, 7.2, 7.6)."""
from __future__ import annotations

import ast
import asyncio
import re
import threading
from datetime import datetime, timedelta, timezone

import pytest

from coroner.evidence.schema import validate_sample
from coroner.probe import probe as probe_mod
from coroner.probe.probe import NO_CONTAINER_ID_REASON, Probe, ProbeConfig

H64 = "3f1c0e0a9d7b4c1e8a6f5d2b9c4e7a1033b8d5f6e2a4c7b9d1e3f5a7c9b1d3e5"
KEYS = {
    "run_id", "seq", "ts", "rss_bytes", "request_count", "loop_delay_ms", "pid",
    "container_id", "container_id_source", "interval_ms", "dropped",
}
WALL = datetime(2025, 1, 1, 12, 0, 6, 512345, tzinfo=timezone.utc)
ORIG_SLEEP = asyncio.sleep


class NullTransport:
    def send(self, body, timeout_s):
        return True


def make(interval=500, **kw):
    kw.setdefault("transport", NullTransport())
    kw.setdefault("rss_reader", lambda: 71303168)
    kw.setdefault("pid_reader", lambda: 7)
    kw.setdefault("container_id_reader", lambda: (H64, "mountinfo"))
    kw.setdefault("wall_clock", lambda: WALL)
    return Probe(ProbeConfig(run_id="r1", collector_url="http://x", interval_ms=interval), **kw)


def ok(sample):
    assert validate_sample(sample) == []
    return sample


def boom():
    raise OSError("/proc/self/statm is secret")


# ---- A: Sample construction ----

def test_first_sample_fields_and_exact_keys():
    p = make()
    s = ok(p.build_sample(0.84))
    assert set(s) == KEYS
    assert (s["run_id"], s["seq"], s["ts"], s["rss_bytes"], s["request_count"], s["loop_delay_ms"]) == (
        "r1", 0, "2025-01-01T12:00:06.512Z", 71303168, 0, 0.84)
    assert (s["pid"], s["container_id"], s["container_id_source"], s["interval_ms"], s["dropped"]) == (
        7, H64, "mountinfo", 500, 0)


def test_seq_increments_per_sample_and_state():
    p = make()
    assert p.state == {"next_seq": 0, "dropped": 0}
    assert [ok(p.build_sample(1.0))["seq"] for _ in range(3)] == [0, 1, 2]
    assert p.state == {"next_seq": 3, "dropped": 0}


def test_ts_is_the_injected_utc_clock_converted_with_ms_precision():
    tz = timezone(timedelta(hours=5))
    p = make(wall_clock=lambda: datetime(2025, 3, 4, 10, 0, 1, 999999, tzinfo=tz))
    s = ok(p.build_sample(0.0))
    assert s["ts"] == "2025-03-04T05:00:01.999Z" and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", s["ts"])


def test_hostname_container_id():
    s = ok(make(container_id_reader=lambda: ("0123456789ab", "hostname")).build_sample(1.0))
    assert (s["container_id"], s["container_id_source"]) == ("0123456789ab", "hostname")
    assert "null_reasons" not in s


def test_no_container_id_gives_null_with_reason():
    s = ok(make(container_id_reader=lambda: (None, "none")).build_sample(1.0))
    assert (s["container_id"], s["container_id_source"]) == (None, "none")
    assert s["null_reasons"] == {"container_id": NO_CONTAINER_ID_REASON}


def test_rss_failure_is_null_with_reason_not_zero():
    s = ok(make(rss_reader=boom).build_sample(1.0))
    assert s["rss_bytes"] is None and s["rss_bytes"] != 0
    reason = s["null_reasons"]["rss_bytes"]
    assert 1 <= len(reason) <= 200 and "secret" not in reason and "OSError" in reason


def test_invalid_rss_value_is_null_not_substituted():
    s = ok(make(rss_reader=lambda: True).build_sample(1.0))
    assert s["rss_bytes"] is None and "rss_bytes" in s["null_reasons"]


def test_container_reader_exception_is_null_with_reason():
    s = ok(make(container_id_reader=boom).build_sample(1.0))
    assert s["container_id"] is None and s["container_id_source"] == "none"
    assert 1 <= len(s["null_reasons"]["container_id"]) <= 200


def test_null_loop_delay_with_reason():
    s = ok(make().build_sample(None, "loop clock unavailable"))
    assert s["loop_delay_ms"] is None and s["null_reasons"] == {"loop_delay_ms": "loop clock unavailable"}


@pytest.mark.parametrize("reason", [None, "", "x" * 201, 5])
def test_null_loop_delay_without_valid_reason_raises_and_keeps_seq(reason):
    p = make()
    with pytest.raises(ValueError):
        p.build_sample(None, reason)
    assert p.state["next_seq"] == 0


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True, "1"])
def test_invalid_loop_delay_raises(bad):
    p = make()
    with pytest.raises(ValueError):
        p.build_sample(bad)
    assert p.state["next_seq"] == 0


def test_several_null_reasons_at_once():
    s = ok(make(rss_reader=boom, container_id_reader=lambda: (None, "none")).build_sample(None, "why"))
    assert set(s["null_reasons"]) == {"rss_bytes", "container_id", "loop_delay_ms"}


@pytest.mark.parametrize("delay", [0.0, 0, 0.001, 123.456, 1e6])
def test_finite_loop_delays_preserved_exactly(delay):
    assert ok(make().build_sample(delay))["loop_delay_ms"] == delay


@pytest.mark.parametrize("pid", [boom, lambda: True, lambda: "1", lambda: None, lambda: -1])
def test_unusable_pid_raises_and_never_substitutes(pid):
    p = make(pid_reader=pid)
    with pytest.raises((OSError, ValueError)):
        p.build_sample(1.0)
    assert p.state["next_seq"] == 0


# ---- B: request count ----

def test_request_count_starts_zero_and_is_captured():
    p = make()
    assert p.build_sample(1.0)["request_count"] == 0
    for _ in range(3):
        p.count_request()
    assert p.build_sample(1.0)["request_count"] == 3


def test_concurrent_request_counting_loses_nothing():
    p = make()
    barrier = threading.Barrier(8)

    def work():
        barrier.wait()
        for _ in range(1000):
            p.count_request()

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert p.build_sample(1.0)["request_count"] == 8000


# ---- C: schedule with a fake clock ----

class Clock:
    """Fake monotonic clock; patched asyncio.sleep advances it by the requested delay plus scripted jitter."""

    def __init__(self, start=1000.0, extras=(), early_first=False):
        self.t, self.extras, self.early_first, self.sleeps = start, list(extras), early_first, []

    def now(self):
        return self.t

    async def sleep(self, delay):
        self.sleeps.append(delay)
        if self.early_first and len(self.sleeps) == 1:
            self.t += delay / 2
        else:
            self.t += delay + (self.extras.pop(0) if self.extras else 0.0)
        await ORIG_SLEEP(0)


async def run_samples(p, n):
    got = []
    p._on_sample = got.append
    p.start()
    for _ in range(100000):
        if len(got) >= n:
            break
        await ORIG_SLEEP(0)
    await p.stop()
    return got


def drive(monkeypatch, clock, n, interval=500, **kw):
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    p = make(interval, monotonic=clock.now, **kw)
    return asyncio.run(run_samples(p, n)), p


@pytest.mark.parametrize("interval", [100, 500, 60000])
def test_exact_deadlines_give_zero_delay_and_seq_runs_on(monkeypatch, interval):
    clock = Clock()
    got, _ = drive(monkeypatch, clock, 4, interval)
    assert [s["seq"] for s in got[:4]] == [0, 1, 2, 3]
    assert [s["loop_delay_ms"] for s in got[:4]] == [0.0] * 4
    assert clock.sleeps[0] == pytest.approx(interval / 1000)
    assert all(ok(s) for s in got)


def test_early_wake_sleeps_again_and_never_produces_early(monkeypatch):
    clock = Clock(early_first=True)
    got, _ = drive(monkeypatch, clock, 1)
    assert len(clock.sleeps) >= 2 and clock.sleeps[1] == pytest.approx(0.25)
    assert got[0]["loop_delay_ms"] == 0.0 and clock.t >= 1000.5


def test_late_wake_reports_lateness_in_ms(monkeypatch):
    got, _ = drive(monkeypatch, Clock(extras=[0.0, 0.125]), 3)
    assert got[0]["loop_delay_ms"] == 0.0
    assert got[1]["loop_delay_ms"] == pytest.approx(125.0)
    assert got[2]["loop_delay_ms"] == pytest.approx(0.0, abs=1e-6)  # back on the t0 + k*interval grid


def test_missed_intervals_skip_forward_without_burst(monkeypatch):
    clock = Clock(extras=[1.2])  # first tick (due +0.5) wakes at +1.7: deadlines +1.0 and +1.5 were missed
    got, _ = drive(monkeypatch, clock, 3)
    assert [s["seq"] for s in got[:3]] == [0, 1, 2]  # exactly one Sample for the late tick
    assert got[0]["loop_delay_ms"] == pytest.approx(1200.0)
    assert clock.sleeps[1] == pytest.approx(0.3)  # next future deadline is +2.0, not an immediate burst
    assert got[1]["loop_delay_ms"] == pytest.approx(0.0, abs=1e-6)


def test_no_cumulative_drift_with_jitter(monkeypatch):
    jitter = [0.001 * ((i * 7) % 11) for i in range(200)]
    clock = Clock(extras=jitter)
    deadlines = []
    got, _ = drive(monkeypatch, clock, 150)
    for s in got[:150]:
        deadlines.append(s["loop_delay_ms"])
    # every wake-up lies in [deadline, deadline + 10 ms]: lateness never accumulates
    assert all(0.0 <= d <= 10.5 for d in deadlines)
    assert [s["seq"] for s in got[:150]] == list(range(150))


def test_construction_failure_emits_nothing_and_keeps_seq(monkeypatch):
    calls = {"n": 0}

    def flaky_pid():
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("pid")
        return 1

    got, p = drive(monkeypatch, Clock(), 3, pid_reader=flaky_pid)
    assert [s["seq"] for s in got[:3]] == [0, 1, 2] and calls["n"] >= 4
    assert all(ok(s) for s in got)


def test_failing_consumer_does_not_stop_the_loop(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    seen = []

    def consumer(s):
        seen.append(s)
        raise RuntimeError("offer failed")

    p = make(monotonic=clock.now, on_sample=consumer)

    async def go():
        p.start()
        while len(seen) < 3:
            await ORIG_SLEEP(0)
        await p.stop()

    asyncio.run(go())
    assert [s["seq"] for s in seen[:3]] == [0, 1, 2]


def test_stop_cancels_cleanly_and_start_is_idempotent(monkeypatch):
    async def blocked(delay):
        await asyncio.get_running_loop().create_future()

    monkeypatch.setattr(asyncio, "sleep", blocked)
    p = make(monotonic=lambda: 0.0)

    async def go():
        await p.stop()  # not started
        p.start()
        first = p._task
        p.start()
        assert p._task is first
        await ORIG_SLEEP(0)
        await p.stop()
        assert first.done() and first.cancelled()
        await p.stop()  # already stopped

    asyncio.run(go())


def test_probe_module_has_no_shutdown_hooks_or_third_party_imports():
    tree = ast.parse(open(probe_mod.__file__, encoding="utf-8").read())
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not mods & {"signal", "atexit", "coroner"}


# ---- D36 regression: no Sample for an unusable pid or a naive clock; seq is not consumed ----

@pytest.mark.parametrize("bad_pid", [boom, lambda: True, lambda: "7"])
def test_pid_failure_skips_the_tick_without_consuming_seq(monkeypatch, bad_pid):
    state = {"bad": True}
    got, p = drive(monkeypatch, Clock(), 2, pid_reader=lambda: bad_pid() if state.pop("bad", False) else 7)
    assert [s["seq"] for s in got[:2]] == [0, 1] and all(s["pid"] == 7 for s in got)


def test_naive_wall_clock_is_rejected_not_read_as_local_time():
    p = make(wall_clock=lambda: datetime(2025, 1, 1, 12, 0, 0))
    with pytest.raises(ValueError):
        p.build_sample(1.0)
    assert p.state["next_seq"] == 0
