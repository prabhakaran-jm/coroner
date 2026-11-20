"""Task 4.4: acceptance-level unit suite for the Probe (Requirements 2.2, 2.3, 2.5, 2.6, 2.9, 2.11, 3.1, 3.5, 3.6, 13.1).

Docker-free and network-free: every file, clock, transport and HTTP connection is injected or faked,
and correctness never depends on a real-time sleep (Events, barriers and scripted clocks only; the
polling helper wait_idle only bounds how long a failing test may wait).

Coverage map (area -> tests in this file; cross-checks also exist in test_probe_config_readers.py,
test_probe_sampler.py and test_probe_sender.py):

  A Config            test_cfg_*            (D35: one warning per invalid supplied interval)
  B RSS reader        test_rss_*
  C Container id      test_cid_*
  D Sample build      test_sample_*         (D36: failed construction consumes no seq)
  E Scheduler         test_sched_*          (fake loop clock, absolute deadlines)
  F Sender/one slot   test_sender_*
  G Delivery deadline test_deadline_*, test_http_*
  H Dropped counter   test_dropped_*

Delivery-deadline limitation (not relaxed, not hidden): the real HTTP transport bounds each phase with a
socket timeout of the remaining budget and the sender refuses to acknowledge a late result, but DNS
(getaddrinfo) ignores socket timeouts. These tests therefore do NOT claim an unconditional 200 ms
wall-clock bound for the real transport; they check the budget handed to the transport and the
acknowledgement rule. Float tolerance for "remaining <= budget": relative 1e-9 (TOL).
"""
from __future__ import annotations

import asyncio
import dataclasses
import http.client
import json
import re
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from coroner.evidence.schema import validate_sample
from coroner.probe import probe as probe_mod
from coroner.probe.probe import (
    NO_CONTAINER_ID_REASON,
    Probe,
    ProbeConfig,
    load_config,
    read_container_id,
    read_rss_bytes,
)

H64 = "3f1c0e0a9d7b4c1e8a6f5d2b9c4e7a1033b8d5f6e2a4c7b9d1e3f5a7c9b1d3e5"
H64_B = "a" * 64
H12 = "0123456789ab"
KEYS = {
    "run_id", "seq", "ts", "rss_bytes", "request_count", "loop_delay_ms", "pid",
    "container_id", "container_id_source", "interval_ms", "dropped",
}
WALL = datetime(2025, 1, 1, 12, 0, 6, 512345, tzinfo=timezone.utc)
ORIG_SLEEP = asyncio.sleep
TOL = 1e-9
MAX_U32 = 4294967295


def ok(sample):
    assert validate_sample(sample) == [], sample
    return sample


def boom():
    raise OSError("/proc/self/statm is secret")


# ------------------------------------------------------------------ A: config


def cfg(env):
    warnings = []
    return load_config(env, warnings.append), warnings


def test_cfg_missing_interval_is_500_without_warning():
    c, w = cfg({"CORONER_RUN_ID": "r1"})
    assert c.interval_ms == 500 and w == []


@pytest.mark.parametrize("raw, expected", [("100", 100), ("60000", 60000), ("500", 500), ("1234", 1234)])
def test_cfg_valid_interval_boundaries_accepted_silently(raw, expected):
    c, w = cfg({"CORONER_RUN_ID": "r1", "CORONER_SAMPLE_INTERVAL_MS": raw})
    assert c.interval_ms == expected and w == []


@pytest.mark.parametrize(
    "raw", ["99", "60001", "0", "-5", "abc", "1.5", "100ms", " 100", "+100", "", "   ", "\t", "\u0661\u0660\u0660"]
)
def test_cfg_invalid_interval_falls_back_to_500_with_exactly_one_warning(raw):
    c, w = cfg({"CORONER_RUN_ID": "r1", "CORONER_SAMPLE_INTERVAL_MS": raw})
    assert c.interval_ms == 500
    assert len(w) == 1 and repr(raw) in w[0]


def test_cfg_one_warning_per_invalid_supplied_value():
    w = []
    for raw in ("99", "60001", "text"):
        load_config({"CORONER_RUN_ID": "r1", "CORONER_SAMPLE_INTERVAL_MS": raw}, w.append)
    assert len(w) == 3


def test_cfg_explicit_valid_run_id_is_used():
    assert cfg({"CORONER_RUN_ID": "run-1.A_b"})[0].run_id == "run-1.A_b"


@pytest.mark.parametrize("bad", ["-a", "a b", "x" * 65, "a/b", "\u00e9", "a\n"])
def test_cfg_invalid_explicit_run_id_raises_value_error(bad):
    with pytest.raises(ValueError):
        cfg({"CORONER_RUN_ID": bad})


@pytest.mark.parametrize("env", [{}, {"CORONER_RUN_ID": ""}])
def test_cfg_generated_run_id_is_manual_plus_8_hex(env):
    assert re.fullmatch(r"manual-[0-9a-f]{8}", cfg(env)[0].run_id)


def test_cfg_is_frozen_with_documented_defaults():
    c, _ = cfg({})
    assert c.collector_url == "http://collector:8081"
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.interval_ms = 1


def test_cfg_reads_only_the_given_mapping_and_never_mutates_it(monkeypatch):
    monkeypatch.setenv("CORONER_SAMPLE_INTERVAL_MS", "7")
    monkeypatch.setenv("CORONER_RUN_ID", "from-os-environ")
    env = {"CORONER_RUN_ID": "r1", "CORONER_SAMPLE_INTERVAL_MS": "250"}
    before = dict(env)
    c, w = cfg(env)
    assert env == before and (c.run_id, c.interval_ms, w) == ("r1", 250, [])


# ------------------------------------------------------------------ B: RSS reader


def statm(tmp_path, text):
    p = tmp_path / "statm"
    p.write_bytes(text.encode())
    return p


def test_rss_resident_pages_field_is_used(tmp_path):
    assert read_rss_bytes(statm(tmp_path, "1000 25 3 1 0 20 0\n"), lambda: 4096) == 25 * 4096


@pytest.mark.parametrize("page", [1, 4096, 16384, 65536])
def test_rss_is_pages_times_page_size(tmp_path, page):
    assert read_rss_bytes(statm(tmp_path, "9 7 0 0 0 0 0"), lambda: page) == 7 * page


def test_rss_zero_pages_is_zero_not_missing(tmp_path):
    assert read_rss_bytes(statm(tmp_path, "9 0 0"), lambda: 4096) == 0


def test_rss_missing_statm_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        read_rss_bytes(tmp_path / "absent", lambda: 4096)


@pytest.mark.parametrize("text", ["", "   \n", "100", "a b c", "100 x 1", "100 -5 1", "100 +5 1", "100 1.5 1", "100 \u0662\u0665 1"])
def test_rss_malformed_or_negative_or_non_integer_pages_raise_value_error(tmp_path, text):
    with pytest.raises(ValueError):
        read_rss_bytes(statm(tmp_path, text), lambda: 4096)


def probe_with(**kw):
    kw.setdefault("transport", NullTransport())
    kw.setdefault("rss_reader", lambda: 71303168)
    kw.setdefault("pid_reader", lambda: 7)
    kw.setdefault("container_id_reader", lambda: (H64, "mountinfo"))
    kw.setdefault("wall_clock", lambda: WALL)
    interval = kw.pop("interval", 500)
    timeout = kw.pop("send_timeout_s", 0.2)
    return Probe(ProbeConfig("r1", "http://collector:8081", interval, send_timeout_s=timeout), **kw)


class NullTransport:
    def send(self, body, timeout_s):
        return True


def test_rss_reader_exception_gives_null_with_reason_in_the_sample(tmp_path):
    p = probe_with(rss_reader=lambda: read_rss_bytes(tmp_path / "absent", lambda: 4096))
    s = ok(p.build_sample(1.0))
    assert s["rss_bytes"] is None
    assert 1 <= len(s["null_reasons"]["rss_bytes"]) <= 200


def test_rss_no_previous_value_fallback_after_a_failure(tmp_path):
    good, bad = statm(tmp_path, "9 25 0"), tmp_path / "gone"
    paths = [good, bad]
    p = probe_with(rss_reader=lambda: read_rss_bytes(paths.pop(0), lambda: 4096))
    first, second = ok(p.build_sample(1.0)), ok(p.build_sample(1.0))
    assert first["rss_bytes"] == 25 * 4096
    assert second["rss_bytes"] is None and "rss_bytes" in second["null_reasons"]


# ------------------------------------------------------------------ C: container id


def mountinfo_line(cid):
    return f"36 35 98:0 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw,relatime - ext4 /dev/sda1 rw\n"


def files(tmp_path, mountinfo=None, cgroup=None):
    m, g = tmp_path / "mountinfo", tmp_path / "cgroup"
    if mountinfo is not None:
        m.write_bytes(mountinfo.encode())
    if cgroup is not None:
        g.write_bytes(cgroup.encode())
    return m, g


def cid(tmp_path, mountinfo=None, cgroup=None, hostname=None):
    m, g = files(tmp_path, mountinfo, cgroup)
    return read_container_id(m, g, {} if hostname is None else {"HOSTNAME": hostname})


def test_cid_mountinfo_has_priority_over_cgroup_and_hostname(tmp_path):
    got = cid(tmp_path, "1 2 3:4 / / rw\n" + mountinfo_line(H64), f"0::/docker/{H64_B}\n", H12)
    assert got == (H64, "mountinfo")


@pytest.mark.parametrize("cgroup", [f"12:cpu,cpuacct:/docker/{H64}\n", f"0::/system.slice/docker-{H64}.scope\n", f"0::/docker/{H64}/x\n"])
def test_cid_cgroup_is_the_fallback_when_mountinfo_has_none(tmp_path, cgroup):
    assert cid(tmp_path, "1 2 3:4 / / rw\n", cgroup, H12) == (H64, "cgroup")


def test_cid_hostname_is_the_last_fallback(tmp_path):
    assert cid(tmp_path, "nothing\n", "0::/\n", H12) == (H12, "hostname")


def test_cid_missing_everything_is_none(tmp_path):
    assert cid(tmp_path) == (None, "none")


@pytest.mark.parametrize("bad", [H64[:63], H64 + "0", H64.upper(), "g" * 64])
def test_cid_malformed_or_wrong_length_ids_in_mountinfo_and_cgroup_are_rejected(tmp_path, bad):
    assert cid(tmp_path, mountinfo_line(bad), f"0::/docker/{bad}\n") == (None, "none")


@pytest.mark.parametrize("bad", [H12[:11], H12 + "0", H12.upper(), "g" * 12, "", " " + H12])
def test_cid_hostname_must_be_exactly_12_lowercase_hex(tmp_path, bad):
    assert cid(tmp_path, hostname=bad) == (None, "none")


def test_cid_unrelated_hex_text_is_not_a_container_id(tmp_path):
    text = f"some other mount /srv/{H64}/data and {H64}\n"
    assert cid(tmp_path, text, text) == (None, "none")


def test_cid_wrong_long_mountinfo_falls_through_to_correct_cgroup_source(tmp_path):
    assert cid(tmp_path, mountinfo_line(H64[:63]), f"0::/docker/{H64}\n") == (H64, "cgroup")


def test_cid_reader_never_pads_or_invents_a_value(tmp_path):
    cid_, source = cid(tmp_path, hostname=H12[:11])
    assert cid_ is None and source == "none"


def test_cid_missing_id_gives_null_with_the_documented_reason_in_the_sample(tmp_path):
    m, g = files(tmp_path)
    p = probe_with(container_id_reader=lambda: read_container_id(m, g, {}))
    s = ok(p.build_sample(1.0))
    assert (s["container_id"], s["container_id_source"]) == (None, "none")
    assert s["null_reasons"] == {"container_id": NO_CONTAINER_ID_REASON}


# ------------------------------------------------------------------ D: Sample construction


def test_sample_initial_seq_is_zero_and_increments_per_produced_sample():
    p = probe_with()
    assert p.state["next_seq"] == 0
    assert [ok(p.build_sample(1.0))["seq"] for _ in range(3)] == [0, 1, 2]
    assert p.state["next_seq"] == 3


def test_sample_failed_pid_yields_no_sample_and_does_not_advance_seq():
    pids = [boom, lambda: 7]
    p = probe_with(pid_reader=lambda: pids.pop(0)() if len(pids) > 1 else 7)
    with pytest.raises(OSError):
        p.build_sample(1.0)
    assert p.state["next_seq"] == 0
    assert ok(p.build_sample(1.0))["seq"] == 0


def test_sample_naive_clock_is_rejected_and_does_not_advance_seq():
    p = probe_with(wall_clock=lambda: datetime(2025, 1, 1))
    with pytest.raises(ValueError):
        p.build_sample(1.0)
    assert p.state["next_seq"] == 0


def test_sample_valid_values_are_preserved_with_exact_key_set():
    p = probe_with()
    p.count_request()
    s = ok(p.build_sample(0.84))
    assert set(s) == KEYS  # no unknown fields, no null_reasons when nothing is null
    assert (s["run_id"], s["ts"], s["rss_bytes"], s["request_count"], s["loop_delay_ms"], s["pid"]) == (
        "r1", "2025-01-01T12:00:06.512Z", 71303168, 1, 0.84, 7)
    assert (s["container_id"], s["container_id_source"], s["interval_ms"], s["dropped"]) == (H64, "mountinfo", 500, 0)


def test_sample_rss_failure_is_null_with_a_valid_reason_and_no_zero_substitute():
    s = ok(probe_with(rss_reader=boom).build_sample(1.0))
    assert s["rss_bytes"] is None and set(s["null_reasons"]) == {"rss_bytes"}
    assert "secret" not in s["null_reasons"]["rss_bytes"]


def test_sample_missing_container_id_is_null_with_reason():
    s = ok(probe_with(container_id_reader=lambda: (None, "none")).build_sample(1.0))
    assert s["container_id"] is None and s["container_id_source"] == "none"
    assert s["null_reasons"] == {"container_id": NO_CONTAINER_ID_REASON}


def test_sample_simultaneous_nullable_failures_have_independent_reasons_of_1_to_200_chars():
    p = probe_with(rss_reader=boom, container_id_reader=lambda: (None, "none"))
    s = ok(p.build_sample(None, "loop clock unavailable"))
    reasons = s["null_reasons"]
    assert set(reasons) == {"rss_bytes", "container_id", "loop_delay_ms"}
    assert len(set(reasons.values())) == 3
    assert all(1 <= len(r) <= 200 for r in reasons.values())
    assert s["rss_bytes"] is None and s["container_id"] is None and s["loop_delay_ms"] is None


def test_sample_overlong_exception_names_still_give_reasons_of_at_most_200_chars():
    class E(Exception):
        pass

    E.__name__ = "E" * 500

    def bad():
        raise E()

    s = ok(probe_with(rss_reader=bad, container_id_reader=bad).build_sample(1.0))
    assert all(1 <= len(r) <= 200 for r in s["null_reasons"].values())


def test_sample_no_synthetic_values_for_unusable_readings():
    s = ok(probe_with(rss_reader=lambda: -1, container_id_reader=boom).build_sample(1.0))
    assert s["rss_bytes"] is None and s["container_id"] is None and s["container_id_source"] == "none"


# ------------------------------------------------------------------ E: absolute-deadline scheduler


class Clock:
    """Fake monotonic clock. The patched asyncio.sleep advances it by the delay plus scripted lateness."""

    def __init__(self, start=1000.0, extras=(), early_first=False):
        self.t0 = self.t = start
        self.extras, self.early_first, self.sleeps = list(extras), early_first, []

    def now(self):
        return self.t

    async def sleep(self, delay):
        self.sleeps.append(delay)
        if self.early_first and len(self.sleeps) == 1:
            self.t += delay / 2
        else:
            self.t += delay + (self.extras.pop(0) if self.extras else 0.0)
        await ORIG_SLEEP(0)


async def run_samples(p, n, clock):
    got, times = [], []

    def collect(s):
        got.append(s)
        times.append(clock.t)

    p._on_sample = collect
    p.start()
    for _ in range(200000):
        if len(got) >= n:
            break
        await ORIG_SLEEP(0)
    await p.stop()
    return got, times


def drive(monkeypatch, clock, n, interval=500, **kw):
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    p = probe_with(interval=interval, monotonic=clock.now, **kw)
    got, times = asyncio.run(run_samples(p, n, clock))
    assert len(got) >= n
    return got, times, p


def test_sched_first_deadline_is_start_plus_one_interval(monkeypatch):
    clock = Clock()
    got, times, _ = drive(monkeypatch, clock, 1, interval=250)
    assert clock.sleeps[0] == pytest.approx(0.25) and times[0] == pytest.approx(clock.t0 + 0.25)


def test_sched_exact_deadline_gives_zero_event_loop_delay(monkeypatch):
    got, _, _ = drive(monkeypatch, Clock(), 4)
    assert [s["loop_delay_ms"] for s in got[:4]] == [0.0] * 4


def test_sched_early_wake_produces_no_early_sample(monkeypatch):
    clock = Clock(early_first=True)
    got, times, _ = drive(monkeypatch, clock, 1)
    assert clock.sleeps[1] == pytest.approx(0.25) and times[0] >= clock.t0 + 0.5 - TOL
    assert got[0]["loop_delay_ms"] == 0.0


def test_sched_late_wake_delay_equals_lateness_in_ms(monkeypatch):
    got, _, _ = drive(monkeypatch, Clock(extras=[0.0, 0.125]), 3)
    assert got[1]["loop_delay_ms"] == pytest.approx(125.0)
    assert got[2]["loop_delay_ms"] == pytest.approx(0.0, abs=1e-6)  # back on the grid, lateness did not carry over


def test_sched_several_missed_intervals_give_one_sample_and_skip_forward_without_burst(monkeypatch):
    clock = Clock(extras=[3.2])  # due +0.5, wakes +3.7; missed +1.0 .. +3.5
    got, times, _ = drive(monkeypatch, clock, 3)
    assert [s["seq"] for s in got[:3]] == [0, 1, 2]
    assert got[0]["loop_delay_ms"] == pytest.approx(3200.0)
    assert clock.sleeps[1] == pytest.approx(0.3)  # next deadline is +4.0
    assert times[1] - times[0] == pytest.approx(0.3) and times[2] - times[1] == pytest.approx(0.5)


def test_sched_no_cumulative_drift_with_jitter(monkeypatch):
    jitter = [0.001 * ((i * 7) % 11) for i in range(200)]
    clock = Clock(extras=jitter)
    got, times, _ = drive(monkeypatch, clock, 100)
    for i, t in enumerate(times[:100]):
        assert 0.0 <= t - (clock.t0 + 0.5 * (i + 1)) <= 0.0105  # lateness never accumulates
    assert all(0.0 <= s["loop_delay_ms"] <= 10.5 for s in got[:100])


def test_sched_seq_follows_produced_samples_when_a_tick_fails(monkeypatch):
    calls = {"n": 0}

    def pid():
        calls["n"] += 1
        if calls["n"] in (1, 3):
            raise OSError("pid")
        return 7

    got, _, _ = drive(monkeypatch, Clock(), 3, pid_reader=pid)
    assert [s["seq"] for s in got[:3]] == [0, 1, 2] and calls["n"] >= 5


def test_sched_reader_failure_does_not_stop_sampling(monkeypatch):
    got, _, _ = drive(monkeypatch, Clock(), 4, rss_reader=boom)
    assert [s["seq"] for s in got[:4]] == [0, 1, 2, 3]
    assert all(ok(s)["rss_bytes"] is None for s in got)


@pytest.mark.parametrize("mode", ["transport_raises", "transport_false", "callback_raises"])
def test_sched_delivery_failure_does_not_stop_sampling(monkeypatch, mode):
    class T:
        def send(self, body, timeout_s):
            if mode == "transport_raises":
                raise ConnectionError("down")
            return False

    clock = Clock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    seen = []

    def consumer(s):
        seen.append(s)
        if mode == "callback_raises":
            raise RuntimeError("callback failed")

    p = probe_with(monotonic=clock.now, transport=T(), on_sample=consumer)

    async def go():
        p.start()
        while len(seen) < 4:
            await ORIG_SLEEP(0)
        await p.stop()

    asyncio.run(go())
    assert [s["seq"] for s in seen[:4]] == [0, 1, 2, 3]


def test_sched_stop_during_a_pending_sleep_cancels_cleanly(monkeypatch):
    async def blocked(delay):
        await asyncio.get_running_loop().create_future()

    monkeypatch.setattr(asyncio, "sleep", blocked)
    p = probe_with(monotonic=lambda: 0.0)

    async def go():
        p.start()
        task = p._task
        await ORIG_SLEEP(0)
        assert not task.done()
        await p.stop()
        assert task.cancelled()
        await p.stop()

    asyncio.run(go())


def test_sched_repeated_start_stop_keeps_sampling_and_leaves_no_thread(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    seen = []
    p = probe_with(monotonic=clock.now, on_sample=seen.append)

    async def cycle(target):
        p.start()
        while len(seen) < target:
            await ORIG_SLEEP(0)
        await p.stop()

    for target in (2, 4, 6):
        asyncio.run(cycle(target))
    seqs = [s["seq"] for s in seen]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # one seq space, never reused
    assert not [t for t in threading.enumerate() if t.name == "coroner-probe-sender" and t.is_alive()]


# ------------------------------------------------------------------ fakes for sender / delivery


class Script:
    """Scripted monotonic clock: returns the values in order, then repeats the last one."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class FakeTransport:
    """Records calls; send() waits for `release` (set at construction unless block=True)."""

    def __init__(self, results=(True,), block=False):
        self.calls, self.results = [], list(results)
        self.entered, self.release = threading.Event(), threading.Event()
        if not block:
            self.release.set()

    def hold(self):
        self.entered.clear()
        self.release.clear()

    def send(self, body, timeout_s):
        self.calls.append((body, timeout_s))
        self.entered.set()
        assert self.release.wait(10)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


def wait_idle(p, timeout=10.0):
    end = time.monotonic() + timeout
    while p._slot is not None:
        assert time.monotonic() < end, "sender did not finish"
        threading.Event().wait(0.0005)


@pytest.fixture
def mk():
    made = []

    def factory(transport=None, start=True, **kw):
        kw.setdefault("monotonic", lambda: 100.0)
        p = probe_with(transport=transport, **kw)
        made.append((p, transport))
        if start:
            p._start_sender()
        return p

    yield factory
    for p, t in made:
        if hasattr(t, "release"):
            t.release.set()
        p._exit.set()
        p._wake.set()
        if p._thread is not None:
            p._thread.join(5)


def offer_new(p):
    s = p.build_sample(1.0)
    p.offer(s)
    return s


# ------------------------------------------------------------------ F: sender / one-slot handoff


def test_sender_is_exactly_one_daemon_thread(mk):
    p = mk(FakeTransport())
    for _ in range(3):
        p._start_sender()
    threads = [t for t in threading.enumerate() if t.name == "coroner-probe-sender" and t.is_alive()]
    assert threads == [p._thread] and p._thread.daemon


def test_sender_idle_sender_accepts_and_delivers_the_serialised_sample(mk):
    t = FakeTransport()
    p = mk(t)
    s = offer_new(p)
    wait_idle(p)
    assert len(t.calls) == 1 and json.loads(t.calls[0][0]) == s
    assert p.state["dropped"] == 0


def test_sender_delivery_starts_promptly_under_controlled_scheduling(mk):
    # Handoff at t=10.00, the sender thread first looks at the clock at t=10.05: the transport is called at
    # once with the remaining 0.15 s budget (no waiting for a tick or a batch). Real-time bound is generous.
    t = FakeTransport()
    p = mk(t, monotonic=Script(10.0, 10.05, 10.06))
    started = time.perf_counter()
    offer_new(p)
    assert t.entered.wait(1.0) and time.perf_counter() - started < 1.0
    wait_idle(p)
    assert t.calls[0][1] == pytest.approx(0.15) and p.state["dropped"] == 0


def test_sender_busy_sender_rejects_a_new_sample_and_counts_one_drop(mk):
    t = FakeTransport(block=True)
    p = mk(t)
    offer_new(p)
    assert t.entered.wait(5)
    offer_new(p)
    assert p.state["dropped"] == 1
    t.release.set()
    wait_idle(p)
    assert len(t.calls) == 1 and p.state["dropped"] == 1


def test_sender_in_flight_sample_is_never_replaced(mk):
    t = FakeTransport(block=True)
    p = mk(t)
    first = offer_new(p)
    assert t.entered.wait(5)
    for _ in range(3):
        offer_new(p)
    t.release.set()
    wait_idle(p)
    assert [json.loads(b)["seq"] for b, _ in t.calls] == [first["seq"]]


def test_sender_no_unbounded_queue_blocked_transport_n_offers(mk):
    t = FakeTransport(block=True)
    p = mk(t)
    for _ in range(25):
        offer_new(p)
    assert t.entered.wait(5) and p.state["dropped"] == 24
    t.release.set()
    wait_idle(p)
    assert len(t.calls) == 1 and p.state["dropped"] == 24 and p._slot is None


def test_sender_failed_transport_does_not_stop_future_deliveries(mk):
    t = FakeTransport(results=[False, RuntimeError("boom"), True])
    p = mk(t)
    for _ in range(3):
        offer_new(p)
        wait_idle(p)
    assert len(t.calls) == 3
    assert p.state["dropped"] == 0  # +1 +1, then the third acked with carried 2


def test_sender_callback_exception_does_not_stop_sampling_or_delivery(mk):
    state = {"fail": True}

    def cb(sample):
        if state["fail"]:
            raise RuntimeError("callback")

    t = FakeTransport()
    p = mk(t, on_sample=cb)
    offer_new(p)  # callback raises: dropped, never sent
    assert p.state["dropped"] == 1 and t.calls == []
    state["fail"] = False
    offer_new(p)
    wait_idle(p)
    assert len(t.calls) == 1 and p.state["dropped"] == 0  # second sample carried 1 and was acked


def test_sender_callback_returning_normally_is_not_an_ack(mk):
    t = FakeTransport(block=True)
    p = mk(t, on_sample=lambda s: None)
    p.build_sample(1.0)  # seq consumed; irrelevant
    p._dropped = 3  # white-box: three earlier drops
    offer_new(p)
    assert t.entered.wait(5) and p.state["dropped"] == 3  # only the sender's ack path may lower it
    t.release.set()
    wait_idle(p)
    assert p.state["dropped"] == 0


def test_sender_no_retry_one_transport_call_per_handed_off_sample(mk):
    t = FakeTransport(results=[False])
    p = mk(t)
    offer_new(p)
    wait_idle(p)
    p._exit.set()
    p._wake.set()
    p._thread.join(5)
    assert len(t.calls) == 1 and p.state["dropped"] == 1


def test_sender_offer_never_blocks_on_a_blocked_transport(mk):
    t = FakeTransport(block=True)
    p = mk(t)
    offer_new(p)
    assert t.entered.wait(5)
    samples = [p.build_sample(1.0) for _ in range(5)]
    started = time.perf_counter()
    for s in samples:
        p.offer(s)
    assert (time.perf_counter() - started) / 5 < 0.010


# ------------------------------------------------------------------ G: delivery deadline


def test_deadline_ack_on_true_within_budget_and_remaining_never_exceeds_it(mk):
    t = FakeTransport()
    p = mk(t, monotonic=Script(0.0, 0.05, 0.1))
    offer_new(p)
    wait_idle(p)
    remaining = t.calls[0][1]
    assert remaining == pytest.approx(0.15) and remaining <= 0.2 * (1 + TOL)


def test_deadline_expired_budget_prevents_sending(mk):
    t = FakeTransport()
    p = mk(t, monotonic=Script(0.0, 0.25))
    offer_new(p)
    wait_idle(p)
    assert t.calls == [] and p.state["dropped"] == 1


def test_deadline_response_after_the_budget_is_not_acknowledged(mk):
    t = FakeTransport(results=[True])
    p = mk(t, monotonic=Script(0.0, 0.05, 0.25))
    offer_new(p)
    wait_idle(p)
    assert len(t.calls) == 1 and p.state["dropped"] == 1


def test_deadline_response_exactly_at_the_budget_is_acknowledged(mk):
    t = FakeTransport(results=[True])
    p = mk(t, monotonic=Script(0.0, 0.0, 0.2))
    offer_new(p)
    wait_idle(p)
    assert p.state["dropped"] == 0


@pytest.mark.parametrize("result", [False, None, 1, "true", ConnectionError("x"), TimeoutError("t")])
def test_deadline_only_true_acknowledges(mk, result):
    p = mk(FakeTransport(results=[result]))
    offer_new(p)
    wait_idle(p)
    assert p.state["dropped"] == 1


def test_deadline_delayed_transport_does_not_block_the_sampler(monkeypatch):
    main = threading.get_ident()
    clock = Clock()
    # The sampler thread sees the fake loop clock; the sender thread sees a fixed time inside the budget.
    mono = lambda: clock.t if threading.get_ident() == main else 1000.0  # noqa: E731
    t = FakeTransport(block=True)
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    seen = []
    p = probe_with(monotonic=mono, transport=t, on_sample=seen.append)

    async def go():
        p.start()
        while len(seen) < 5:
            await ORIG_SLEEP(0)
        t.release.set()
        await p.stop()

    asyncio.run(go())
    assert [s["seq"] for s in seen[:5]] == [0, 1, 2, 3, 4]
    assert len(t.calls) == 1 and p._thread is not None


def install_http(monkeypatch, status=200, fail=None, clock=None, advance=0.0):
    record = []

    class Sock:
        def settimeout(self, t):
            record.append(("sock", t))

    class Conn:
        def __init__(self, host, port=None, timeout=None):
            record.append(("init", host, port, timeout))
            self.sock = None

        def connect(self):
            if fail == "connect":
                raise ConnectionRefusedError("refused")
            self.sock = Sock()

        def request(self, method, path, body=None, headers=None):
            if fail == "request":
                raise TimeoutError("timed out")
            record.append(("request", method, path, body, headers))

        def getresponse(self):
            if fail == "response":
                raise TimeoutError("timed out")
            if clock is not None:
                clock.t += advance
            return SimpleNamespace(status=status)

        def close(self):
            record.append(("close",))

    monkeypatch.setattr(http.client, "HTTPConnection", Conn)
    return record


def real_send(timeout_s=0.2):
    return probe_mod._HttpTransport("http://collector:8081").send(b"{}", timeout_s)


@pytest.mark.parametrize(
    "status, acked",
    [(200, True), (201, False), (204, False), (301, False), (302, False), (400, False), (404, False),
     (422, False), (429, False), (500, False), (503, False)],
)
def test_http_only_status_200_is_acknowledged(monkeypatch, status, acked):
    rec = install_http(monkeypatch, status=status)
    assert real_send() is acked
    post = [r for r in rec if r[0] == "request"][0]
    assert post[1:3] == ("POST", "/v1/samples") and ("close",) in rec


@pytest.mark.parametrize("fail", ["connect", "request", "response"])
def test_http_connection_errors_and_timeouts_are_not_acknowledged(monkeypatch, fail):
    install_http(monkeypatch, fail=fail)
    assert real_send() is False


def test_http_expired_budget_never_opens_a_connection(monkeypatch):
    rec = install_http(monkeypatch)
    assert real_send(0.0) is False and rec == []


def test_http_response_after_200_ms_is_not_acknowledged_and_timeouts_stay_within_budget(monkeypatch):
    clock = Clock()
    rec = install_http(monkeypatch, status=200, clock=clock, advance=0.25)
    monkeypatch.setattr(probe_mod, "time", SimpleNamespace(monotonic=clock.now))
    assert real_send(0.2) is False
    timeouts = [r[3] if r[0] == "init" else r[1] for r in rec if r[0] in ("init", "sock")]
    assert timeouts and all(x <= 0.2 * (1 + TOL) for x in timeouts)


def test_http_end_to_end_204_is_a_failed_delivery_and_200_is_an_ack(monkeypatch, mk):
    install_http(monkeypatch, status=204)
    p = mk(None)
    offer_new(p)
    wait_idle(p)
    assert p.state["dropped"] == 1
    install_http(monkeypatch, status=200)
    offer_new(p)  # carried 1
    wait_idle(p)
    assert p.state["dropped"] == 0


# ------------------------------------------------------------------ H: dropped counter


def test_dropped_starts_at_zero_and_state_reflects_counter_and_next_seq(mk):
    p = mk(FakeTransport())
    assert p.state == {"next_seq": 0, "dropped": 0}
    offer_new(p)
    wait_idle(p)
    assert p.state == {"next_seq": 1, "dropped": 0}


def test_dropped_plus_one_on_failed_delivery(mk):
    p = mk(FakeTransport(results=[False]))
    offer_new(p)
    wait_idle(p)
    assert p.state["dropped"] == 1


def test_dropped_plus_one_on_busy_discard(mk):
    t = FakeTransport(block=True)
    p = mk(t)
    offer_new(p)
    assert t.entered.wait(5)
    offer_new(p)
    assert p.state["dropped"] == 1


def test_dropped_plus_one_on_serialization_failure_and_no_transport_call(mk):
    t = FakeTransport()
    p = mk(t)
    s = p.build_sample(1.0)
    s["rss_bytes"] = float("nan")
    p.offer(s)
    assert p.state["dropped"] == 1 and p._slot is None and t.calls == []


def test_dropped_plus_one_on_callback_failure(mk):
    def cb(sample):
        raise RuntimeError("x")

    p = mk(FakeTransport(), on_sample=cb)
    offer_new(p)
    assert p.state["dropped"] == 1


@pytest.mark.parametrize("path", ["callback", "serialization", "transport"])
def test_dropped_saturates_at_uint32_max(mk, path):
    def cb(sample):
        raise RuntimeError("x")

    t = FakeTransport(results=[False])
    p = mk(t, on_sample=cb if path == "callback" else None)
    with p._lock:  # white-box: the counter cannot realistically be driven to 2**32 - 1 by offers
        p._dropped = MAX_U32
    s = p.build_sample(1.0)
    if path == "serialization":
        s["rss_bytes"] = float("nan")
    p.offer(s)
    wait_idle(p)
    assert p.state["dropped"] == MAX_U32 and ok(p.build_sample(1.0))["dropped"] == MAX_U32


def test_dropped_saturation_with_a_patched_maximum(monkeypatch, mk):
    monkeypatch.setattr(probe_mod, "MAX_DROPPED", 3)

    def cb(sample):
        raise RuntimeError("x")

    p = mk(FakeTransport(), on_sample=cb)
    for _ in range(6):
        offer_new(p)
    assert p.state["dropped"] == 3


def test_dropped_failed_delivery_never_reduces_the_counter(mk):
    p = mk(FakeTransport(results=[False]))
    seen = []
    for _ in range(4):
        offer_new(p)
        wait_idle(p)
        seen.append(p.state["dropped"])
    assert seen == [1, 2, 3, 4]


def test_dropped_exact_sequence_ack_subtracts_only_the_carried_value(mk):
    t = FakeTransport(results=[True, True, False, True], block=True)
    p = mk(t)
    s0 = offer_new(p)  # carries 0, in flight
    assert t.entered.wait(5) and s0["dropped"] == 0
    offer_new(p)
    offer_new(p)  # two busy drops during flight
    assert p.state["dropped"] == 2
    t.release.set()
    wait_idle(p)
    assert p.state["dropped"] == 2  # ack(carried 0) erases nothing
    t.hold()
    s3 = offer_new(p)  # carries 2
    assert s3["dropped"] == 2 and t.entered.wait(5)
    offer_new(p)  # busy drop -> 3
    assert p.state["dropped"] == 3
    t.release.set()
    wait_idle(p)
    assert p.state["dropped"] == 1  # 3 - carried 2: the newer drop survives
    s5 = offer_new(p)  # carries 1, transport says False
    wait_idle(p)
    assert s5["dropped"] == 1 and p.state["dropped"] == 2
    s6 = offer_new(p)  # carries 2, acked
    wait_idle(p)
    assert s6["dropped"] == 2 and p.state["dropped"] == 0
    assert p.state["next_seq"] == 7  # seven built samples; drops never consumed seq


def test_dropped_no_double_counting_when_the_transport_raises(mk):
    p = mk(FakeTransport(results=[RuntimeError("boom")]))
    offer_new(p)
    wait_idle(p)
    assert p.state["dropped"] == 1


def test_dropped_no_double_counting_when_callback_raises_after_serialization(mk):
    t = FakeTransport()

    def cb(sample):
        raise RuntimeError("x")

    p = mk(t, on_sample=cb)
    offer_new(p)
    assert p.state["dropped"] == 1 and t.calls == []


def hammer(p, threads=8, per_thread=200):
    base = p.build_sample(1.0)
    base["dropped"] = 0  # every sample carries 0, so an ack subtracts nothing
    barrier = threading.Barrier(threads)

    def work():
        barrier.wait()
        for _ in range(per_thread):
            p.offer(dict(base))

    ts = [threading.Thread(target=work) for _ in range(threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wait_idle(p)
    return threads * per_thread


def test_dropped_concurrent_offers_against_acking_sender_match_an_independent_tally(mk):
    t = FakeTransport(results=[True])
    p = mk(t)
    total = hammer(p)
    assert p.state["dropped"] == total - len(t.calls)  # busy drops only; acks subtracted carried 0
    assert 1 <= len(t.calls) <= total


def test_dropped_concurrent_offers_with_failing_sender_count_every_offer_once(mk):
    t = FakeTransport(results=[False])
    p = mk(t)
    total = hammer(p)
    assert p.state["dropped"] == total  # each offer is either a busy drop or one failed delivery


def test_dropped_concurrent_offers_with_mixed_outcomes_match_the_tally(mk):
    class Alternating(FakeTransport):
        failures = 0

        def send(self, body, timeout_s):
            n = len(self.calls)
            result = super().send(body, timeout_s)
            if n % 2:
                self.failures += 1
                return False
            return result

    t = Alternating(results=[True])
    p = mk(t)
    total = hammer(p)
    assert p.state["dropped"] == (total - len(t.calls)) + t.failures
