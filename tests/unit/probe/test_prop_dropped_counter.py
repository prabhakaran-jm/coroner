"""Property 18: Dropped counter accounting (Requirements 3.3, 3.4, 3.5, 3.7; D9, D35, D36).

The REAL Probe runs with its real sender thread (started through the same `_start_sender` helper the other
probe tests use). Readers and both clocks are injected and deterministic; the transport is a gate: it sets
`entered` when `send` begins and blocks on `release`, so the slot stays occupied while the test offers more
Samples (busy discards) and then resolves the in-flight attempt with one chosen outcome. No sleeps: waits
are Event waits or bounded spins.

Sync method (white-box, documented): the sender clears `_slot` and updates `_dropped` in ONE lock section,
so "attempt finished" is `Probe._slot is None`; the test spins on it with a 5 s bound.

An independent reference model (`d`, `seq`, `carried`) is computed from the generated actions only and never
calls a Probe counter helper:
  discard (busy / callback raised / serialisation failed) -> min(MAX, d + 1), in-flight Sample kept;
  failed delivery (error / timeout / non-200 / late ack)   -> min(MAX, d + 1);
  ack (HTTP 200 within the deadline)                       -> max(0, d - carried), carried = value in the Sample.
Initial `dropped` (including near MAX) is seeded white-box under the Probe lock; that is the only seeding.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timezone

import pytest
from hypothesis import event, example, given, note, settings
from hypothesis import strategies as st

from coroner.probe import probe as probe_mod
from coroner.probe.probe import MAX_DROPPED, Probe, ProbeConfig, _HttpTransport

SETTINGS = dict(derandomize=True, database=None, deadline=None)
BOUND = 5.0
HTTP_STATUSES = [200, 201, 204, 301, 404, 500, 503]
OUTCOMES = ["ack", "err_raise", "err_false", "sockto", "late"] + [f"http:{s}" for s in HTTP_STATUSES]


def is_ack(outcome: str) -> bool:
    return outcome == "ack" or outcome == "http:200"


class Conn:
    """http.client.HTTPConnection double; `status` is set by the gate for the current attempt."""

    status = 200

    def __init__(self, host, port=None, timeout=None):
        self.sock = self

    def settimeout(self, value):
        pass

    def connect(self):
        pass

    def request(self, method, path, body, headers):
        pass

    def getresponse(self):
        return type("R", (), {"status": Conn.status})()

    def close(self):
        pass


class Gate:
    """Blocking transport: records every call, waits for `release`, then applies `outcome`."""

    def __init__(self, clock):
        self.clock, self.calls, self.outcome = clock, [], "ack"
        self.entered, self.release = threading.Event(), threading.Event()

    def reset(self):
        self.entered, self.release = threading.Event(), threading.Event()

    def send(self, body, timeout_s):
        self.calls.append((body, timeout_s))
        self.entered.set()
        assert self.release.wait(BOUND)
        o = self.outcome
        if o == "ack":
            return True
        if o == "err_raise":
            raise OSError("boom")
        if o == "err_false":
            return False
        if o == "sockto":
            raise socket.timeout("timed out")
        if o == "late":
            self.clock[0] += 0.3  # injected monotonic now shows > 200 ms; transport still says True
            return True
        Conn.status = int(o.split(":")[1])
        return _HttpTransport("http://c:1").send(body, timeout_s)


def wait_idle(p: Probe) -> None:
    end = time.monotonic() + BOUND
    while p._slot is not None:  # white-box sync: slot clearing and accounting share one lock section
        assert time.monotonic() < end, "sender did not finish the attempt"
        time.sleep(0)


def run(initial: int, steps: list, emit=None):
    """Drive the real Probe through `steps`, checking the independent model after every action.

    Returns (final state, gate). Steps: ("deliver", outcome, n_busy), ("cbfail",), ("serfail",), ("badbuild", "pid"|"naive").
    """
    clock, flags = [0.0], {"pid_fail": False, "naive": False, "cb_fail": False}

    def pid():
        if flags["pid_fail"]:
            raise OSError("no pid")
        return 7

    def wall():
        return datetime(2025, 1, 1) if flags["naive"] else datetime(2025, 1, 1, tzinfo=timezone.utc)

    def cb(sample):
        if flags["cb_fail"]:
            raise RuntimeError("callback")

    gate = Gate(clock)
    p = Probe(ProbeConfig(run_id="r1", collector_url="http://c:1", interval_ms=500), transport=gate,
              rss_reader=lambda: 1, pid_reader=pid, container_id_reader=lambda: (None, "none"),
              monotonic=lambda: clock[0], wall_clock=wall, on_sample=cb)
    with p._lock:  # white-box seed of the initial counter
        p._dropped = initial
    d, seq, attempts, seen_seqs = initial, 0, 0, []
    original_conn = probe_mod.http.client.HTTPConnection
    probe_mod.http.client.HTTPConnection = Conn
    p._start_sender()

    def check(what):
        s = p.state
        assert s["dropped"] == d and s["next_seq"] == seq, f"{what}: probe={s} model d={d} seq={seq}"
        assert 0 <= s["dropped"] <= MAX_DROPPED
        assert len(gate.calls) == attempts, f"{what}: transport calls {len(gate.calls)} != attempts {attempts}"

    def mark(label):
        if emit:
            emit(label)

    try:
        for step in steps:
            kind = step[0]
            if kind == "badbuild":
                flags["pid_fail" if step[1] == "pid" else "naive"] = True
                with pytest.raises((OSError, ValueError)):
                    p.build_sample(1.0)  # failed construction: seq not consumed
                flags["pid_fail"] = flags["naive"] = False
                check("badbuild")
                continue
            sample = p.build_sample(1.0)
            assert sample["seq"] == seq and sample["dropped"] == d
            seq += 1
            if kind in ("cbfail", "serfail"):
                flags["cb_fail"] = kind == "cbfail"
                if kind == "serfail":
                    sample["x"] = object()
                p.offer(sample)
                flags["cb_fail"] = False
                d = min(MAX_DROPPED, d + 1)
                mark(kind)
                check(kind)
                continue
            _, outcome, n_busy = step
            carried = d
            gate.outcome = outcome
            p.offer(sample)
            assert gate.entered.wait(BOUND), "transport never entered"
            attempts += 1
            sent = json.loads(gate.calls[-1][0])
            seen_seqs.append(sent["seq"])
            assert sent["seq"] == sample["seq"] and sent["dropped"] == carried
            assert gate.calls[-1][1] == pytest.approx(0.2)
            check("in-flight")  # a callback that returned normally is not an ack: d unchanged
            for _ in range(n_busy):
                extra = p.build_sample(1.0)
                assert extra["seq"] == seq and extra["dropped"] == d
                seq += 1
                p.offer(extra)
                d = min(MAX_DROPPED, d + 1)
                check("busy")
                assert json.loads(gate.calls[-1][0])["seq"] == sample["seq"]  # busy Sample never replaces in-flight
            gate.release.set()
            wait_idle(p)
            d = max(0, d - carried) if is_ack(outcome) else min(MAX_DROPPED, d + 1)
            mark(f"outcome:{outcome.split(':')[0] if outcome != 'http:200' else 'ack'}")
            if n_busy:
                mark("busy-during-flight")
                mark(("ack" if is_ack(outcome) else "fail") + "-after-concurrent-drops")
            if d >= MAX_DROPPED - 2:
                mark("at-max" if d == MAX_DROPPED else "near-max")
            check(f"resolved {outcome}")
            gate.reset()
        assert len(set(seen_seqs)) == len(seen_seqs)  # no Sample sent twice
        return p.state, gate
    finally:
        probe_mod.http.client.HTTPConnection = original_conn
        gate.release.set()
        p._exit.set()
        p._wake.set()
        p._thread.join(2)
        assert not p._thread.is_alive()
        assert len(gate.calls) == attempts, "hidden queue or retry: extra sends after the run"


# ---- main property ----------------------------------------------------------------------------------------
initials = st.one_of(
    st.sampled_from([0, 1, 2, 5, MAX_DROPPED - 2, MAX_DROPPED - 1, MAX_DROPPED]),
    st.integers(0, 20),
    st.integers(MAX_DROPPED - 10, MAX_DROPPED),
)
deliver = st.tuples(st.just("deliver"), st.sampled_from(OUTCOMES), st.integers(0, 3))
other = st.one_of(
    st.just(("cbfail",)), st.just(("serfail",)), st.just(("badbuild", "pid")), st.just(("badbuild", "naive")),
)
step_strategy = st.one_of(deliver, deliver, deliver, other)


@settings(max_examples=300, **SETTINGS)
@given(initial=initials, steps=st.lists(step_strategy, min_size=1, max_size=6))
@example(initial=5, steps=[("deliver", "ack", 0)])
@example(initial=5, steps=[("deliver", "ack", 2)])
@example(initial=5, steps=[("deliver", "err_raise", 2)])
@example(initial=0, steps=[("deliver", "late", 1)])
@example(initial=MAX_DROPPED - 1, steps=[("deliver", "http:204", 3), ("deliver", "ack", 0)])
def test_property_18_dropped_counter_accounting(initial, steps):
    note(f"initial={initial} steps={steps}")
    event(f"initial:{'at-max' if initial == MAX_DROPPED else 'near-max' if initial >= MAX_DROPPED - 10 else 'low'}")
    final, gate = run(initial, steps, emit=event)
    assert any(s[0] != "deliver" for s in steps) or len(gate.calls) == len(steps)


# ---- explicit examples A-F (expected values written out, not taken from the model) ------------------------
def test_a_no_pending_drops_ack_stays_zero():
    assert run(0, [("deliver", "ack", 0)])[0] == {"next_seq": 1, "dropped": 0}


def test_b_ack_subtracts_carried_value():
    assert run(5, [("deliver", "ack", 0)])[0]["dropped"] == 0


def test_c_ack_preserves_drops_made_during_flight():
    assert run(5, [("deliver", "ack", 2)])[0] == {"next_seq": 3, "dropped": 2}


def test_d_failure_after_concurrent_drops_adds_one_more():
    assert run(5, [("deliver", "err_raise", 2)])[0]["dropped"] == 8


@pytest.mark.parametrize("outcome", ["http:204", "http:503", "late"])
def test_e_non_200_or_late_ack_counts_one_and_subtracts_nothing(outcome):
    assert run(5, [("deliver", outcome, 0)])[0]["dropped"] == 6


def test_f_next_sample_carries_updated_count_then_acks():
    final, gate = run(5, [("deliver", "err_false", 0), ("deliver", "ack", 0)])
    assert json.loads(gate.calls[1][0])["dropped"] == 6 and final["dropped"] == 0


# ---- saturation (WHITE-BOX: initial counter seeded near MAX; never wraps, never negative) ----------------
@pytest.mark.parametrize("initial,busy", [(MAX_DROPPED - 2, 3), (MAX_DROPPED - 1, 2), (MAX_DROPPED, 2)])
def test_saturation_discards_clamp_at_max(initial, busy):
    # MAX-2 + 3 discards + 1 failure would be MAX+2: every path is clamped
    final = run(initial, [("deliver", "err_raise", busy)])[0]
    assert final["dropped"] == MAX_DROPPED


def test_saturation_ack_of_near_max_with_new_drops_during_ack():
    final = run(MAX_DROPPED - 1, [("deliver", "ack", 2)])[0]
    assert final["dropped"] == 1  # MAX-1 + 2 drops clamps to MAX; ack subtracts the carried MAX-1


def test_saturation_failure_then_ack_never_negative():
    final, gate = run(MAX_DROPPED, [("deliver", "http:500", 1), ("deliver", "ack", 0)])
    assert json.loads(gate.calls[1][0])["dropped"] == MAX_DROPPED and final["dropped"] == 0


# ---- delivery semantics ----------------------------------------------------------------------------------
@pytest.mark.parametrize("status", [201, 204, 301, 404, 500, 503])
def test_only_http_200_acks(status):
    assert run(3, [("deliver", f"http:{status}", 0)])[0]["dropped"] == 4
    assert run(3, [("deliver", "http:200", 0)])[0]["dropped"] == 0


@pytest.mark.parametrize("outcome", ["err_raise", "err_false", "sockto", "late"])
def test_each_failure_kind_counts_exactly_one_and_is_not_retried(outcome):
    final, gate = run(0, [("deliver", outcome, 0)])
    assert final["dropped"] == 1 and len(gate.calls) == 1


def test_no_hidden_queue_after_many_busy_offers():
    final, gate = run(0, [("deliver", "ack", 5)])
    assert len(gate.calls) == 1 and final == {"next_seq": 6, "dropped": 5}


def test_failed_construction_does_not_consume_seq():
    final, _ = run(0, [("badbuild", "pid"), ("badbuild", "naive"), ("deliver", "ack", 0)])
    assert final == {"next_seq": 1, "dropped": 0}


def test_seq_consumed_when_delivery_fails():
    assert run(0, [("deliver", "err_raise", 0), ("deliver", "ack", 0)])[0]["next_seq"] == 2


# ---- callback and serialisation failures ------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["cbfail", "serfail"])
def test_callback_or_serialisation_failure_counts_once_and_later_sample_delivers(kind):
    final, gate = run(0, [(kind,), ("deliver", "ack", 0)])
    assert len(gate.calls) == 1
    assert json.loads(gate.calls[0][0])["dropped"] == 1 and json.loads(gate.calls[0][0])["seq"] == 1
    assert final == {"next_seq": 2, "dropped": 0}


def test_transport_raising_counts_once_and_sender_stays_operational():
    final, _ = run(0, [("deliver", "err_raise", 0), ("deliver", "err_raise", 0), ("deliver", "ack", 0)])
    assert final["dropped"] == 0 and final["next_seq"] == 3
