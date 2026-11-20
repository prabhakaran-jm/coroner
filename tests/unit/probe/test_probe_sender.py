"""Task 4.3: one-slot sender, HTTP transport and dropped accounting (Requirements 3.1 to 3.7, D9, D36). No real network."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from datetime import datetime, timezone

import pytest

from coroner.probe import probe as probe_mod
from coroner.probe.probe import MAX_DROPPED, Probe, ProbeConfig, _HttpTransport

NAME = "coroner-probe-sender"


class Fake:
    """Transport double: records calls, optionally blocks until released, returns or raises `result`."""

    def __init__(self, result=True, block=False):
        self.result, self.block, self.calls = result, block, []
        self.started, self.release = threading.Event(), threading.Event()

    def send(self, body, timeout_s):
        self.calls.append((body, timeout_s))
        self.started.set()
        if self.block:
            assert self.release.wait(5)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def mk():
    made = []

    def build(transport=None, interval=500, **kw):
        kw.setdefault("pid_reader", lambda: 7)
        kw.setdefault("rss_reader", lambda: 1)
        kw.setdefault("container_id_reader", lambda: (None, "none"))
        kw.setdefault("wall_clock", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc))
        p = Probe(ProbeConfig(run_id="r1", collector_url="http://c:1", interval_ms=interval),
                  transport=transport if transport is not None else Fake(), **kw)
        p._start_sender()
        made.append(p)
        return p

    yield build
    for p in made:
        p._exit.set()
        p._wake.set()
        if isinstance(p._transport, Fake):
            p._transport.release.set()
        p._thread.join(2)


def until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.002)


def idle(p):
    until(lambda: p._slot is None)


def send_one(p):
    p.offer(p.build_sample(1.0))
    idle(p)


def dropped(p):
    return p.state["dropped"]


# ---- A: HTTP transport (http.client replaced by a recording double) ----

class Conn:
    instances = []
    status, connect_exc, request_exc, response_exc = 200, None, None, None

    def __init__(self, host, port=None, timeout=None):
        self.args, self.timeout, self.closed, self.sock_timeouts, self.req = (host, port), timeout, False, [], None
        self.sock = self
        Conn.instances.append(self)

    def settimeout(self, value):
        self.sock_timeouts.append(value)

    def connect(self):
        if Conn.connect_exc:
            raise Conn.connect_exc

    def request(self, method, path, body, headers):
        if Conn.request_exc:
            raise Conn.request_exc
        self.req = (method, path, body, headers)

    def getresponse(self):
        if Conn.response_exc:
            raise Conn.response_exc
        return type("R", (), {"status": Conn.status})()

    def close(self):
        self.closed = True


@pytest.fixture
def conn(monkeypatch):
    Conn.instances, Conn.status = [], 200
    Conn.connect_exc = Conn.request_exc = Conn.response_exc = None
    monkeypatch.setattr(probe_mod.http.client, "HTTPConnection", Conn)
    return Conn


BODY = b'{"run_id":"r1","seq":0}'


def test_default_transport_is_http_and_200_is_acknowledged(conn):
    assert isinstance(Probe(ProbeConfig("r1", "http://c:1", 500))._transport, _HttpTransport)
    assert _HttpTransport("http://c:8081").send(BODY, 0.2) is True
    (c,) = conn.instances
    assert c.args == ("c", 8081) and c.closed
    method, path, body, headers = c.req
    assert (method, path) == ("POST", "/v1/samples") and body is BODY
    assert headers == {"Content-Type": "application/json", "Content-Length": str(len(BODY))}
    assert c.timeout <= 0.2 + 1e-6 and c.sock_timeouts and all(0 < t <= 0.2 + 1e-6 for t in c.sock_timeouts)


def test_url_path_prefix_is_kept(conn):
    assert _HttpTransport("http://c:1/base/").send(BODY, 0.2) is True
    assert conn.instances[0].req[1] == "/base/v1/samples"


@pytest.mark.parametrize("status", [201, 202, 204, 301, 302, 400, 422, 429, 500, 503])
def test_only_200_is_acknowledged(conn, status):
    conn.status = status
    assert _HttpTransport("http://c:1").send(BODY, 0.2) is False
    assert len(conn.instances) == 1 and conn.instances[0].closed  # no retry, no redirect following


@pytest.mark.parametrize("where, exc", [
    ("connect_exc", ConnectionRefusedError()), ("connect_exc", socket.timeout()),
    ("request_exc", BrokenPipeError()), ("response_exc", socket.timeout()), ("response_exc", OSError("reset")),
])
def test_network_errors_are_not_acknowledged_and_close_the_connection(conn, where, exc):
    setattr(conn, where, exc)
    assert _HttpTransport("http://c:1").send(BODY, 0.2) is False
    assert len(conn.instances) == 1 and conn.instances[0].closed


@pytest.mark.parametrize("url", ["ftp://c:1", "https://c:1", "http://", "not a url", "http://c:notaport", ""])
def test_invalid_or_unsupported_urls_are_not_acknowledged(conn, url):
    assert _HttpTransport(url).send(BODY, 0.2) is False
    assert all(c.req is None for c in conn.instances)


def test_exhausted_budget_is_not_acknowledged(conn):
    assert _HttpTransport("http://c:1").send(BODY, 0.0) is False
    assert not conn.instances


# ---- B: one-slot sender ----

def test_exactly_one_daemon_sender_thread_and_start_is_idempotent(mk):
    p = mk()
    p._start_sender()
    p._start_sender()
    threads = [t for t in threading.enumerate() if t.name == NAME and t.is_alive()]
    assert threads == [p._thread] and p._thread.daemon


def test_delivery_starts_within_100ms_and_carries_the_original_json(mk):
    t = Fake()
    p = mk(t)
    s = p.build_sample(1.0)
    t0 = time.monotonic()
    p.offer(s)
    assert t.started.wait(0.1) and time.monotonic() - t0 < 0.1
    body, timeout_s = t.calls[0]
    assert body == json.dumps(s, separators=(",", ":"), allow_nan=False).encode() and json.loads(body) == s
    assert 0 < timeout_s <= 0.2 + 1e-6
    idle(p)


def test_offer_never_blocks_even_while_the_transport_is_blocked(mk):
    t = Fake(block=True)
    p = mk(t)
    p.offer(p.build_sample(1.0))
    assert t.started.wait(1)
    worst = 0.0
    for _ in range(20):
        s = p.build_sample(1.0)
        t0 = time.perf_counter()
        p.offer(s)
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.01


def test_busy_sender_rejects_new_samples_and_keeps_the_in_flight_one(mk):
    t = Fake(block=True)
    p = mk(t)
    first = p.build_sample(1.0)
    p.offer(first)
    assert t.started.wait(1)
    for _ in range(5):
        p.offer(p.build_sample(1.0))
    assert dropped(p) == 5 and len(t.calls) == 1 and json.loads(t.calls[0][0])["seq"] == first["seq"] == 0
    t.release.set()
    idle(p)
    assert len(t.calls) == 1  # no queue: nothing is sent later on behalf of the rejected Samples
    assert dropped(p) == 5  # the ack subtracted only the carried 0; drops during flight survive
    send_one(p)
    assert len(t.calls) == 2 and json.loads(t.calls[1][0])["seq"] == 6 and dropped(p) == 0


def test_repeated_start_stop_never_duplicates_the_thread():
    p = Probe(ProbeConfig("r1", "http://c:1", 60000), transport=Fake())

    async def go():
        for _ in range(3):
            p.start()
            p.start()
            assert len([t for t in threading.enumerate() if t.name == NAME and t.is_alive()]) == 1
            await p.stop()
            assert not p._thread.is_alive()
        await p.stop()

    asyncio.run(go())


def test_sampler_keeps_producing_after_failed_deliveries_and_while_the_sender_is_busy():
    failing = Fake(False)
    p = Probe(ProbeConfig("r1", "http://c:1", 100), transport=failing)

    async def run(probe, until_seq):
        probe.start()
        end = time.monotonic() + 5
        while probe.state["next_seq"] < until_seq and time.monotonic() < end:
            await asyncio.sleep(0.01)
        await probe.stop()

    asyncio.run(run(p, 3))
    assert p.state["next_seq"] >= 3 and len(failing.calls) >= 3
    assert [json.loads(b)["seq"] for b, _ in failing.calls][:3] == [0, 1, 2]
    blocked = Fake(block=True)
    q = Probe(ProbeConfig("r1", "http://c:1", 100), transport=blocked)
    blocked.release.clear()
    t = threading.Timer(0.6, blocked.release.set)
    t.start()
    asyncio.run(run(q, 4))
    t.cancel()
    blocked.release.set()
    assert q.state["next_seq"] >= 4 and len(blocked.calls) == 1 and dropped(q) >= 3


# ---- C: dropped accounting ----

def test_initial_dropped_is_zero_and_successful_ack_keeps_it_zero(mk):
    p = mk()
    assert dropped(p) == 0
    send_one(p)
    assert dropped(p) == 0


@pytest.mark.parametrize("result", [False, RuntimeError("x"), OSError("y")])
def test_each_failed_delivery_adds_exactly_one(mk, result):
    t = Fake(result)
    p = mk(t)
    for n in (1, 2, 3):
        send_one(p)
        assert dropped(p) == n
    assert len(t.calls) == 3 and p._thread.is_alive()  # no retry


def test_ack_subtracts_exactly_the_carried_value(mk):
    t = Fake(False)
    p = mk(t)
    for _ in range(3):
        send_one(p)
    assert dropped(p) == 3
    t.result = True
    send_one(p)
    assert json.loads(t.calls[-1][0])["dropped"] == 3 and dropped(p) == 0
    assert [json.loads(b)["seq"] for b, _ in t.calls] == [0, 1, 2, 3]  # seq continues after failures


def test_failed_delivery_of_a_sample_carrying_drops_never_reduces(mk):
    t = Fake(False)
    p = mk(t)
    p._dropped = 4
    send_one(p)
    assert dropped(p) == 5


def test_drops_during_flight_are_preserved_on_ack(mk):
    t = Fake(block=True)
    p = mk(t)
    p._dropped = 5
    p.offer(p.build_sample(1.0))  # carries 5
    assert t.started.wait(1)
    p.offer(p.build_sample(1.0))
    p.offer(p.build_sample(1.0))
    assert dropped(p) == 7
    t.release.set()
    idle(p)
    assert json.loads(t.calls[0][0])["dropped"] == 5 and dropped(p) == 2


def test_ack_with_carried_larger_than_counter_floors_at_zero(mk):
    p = mk()
    p._dropped = 3
    s = p.build_sample(1.0)
    p._dropped = 1
    p.offer(s)
    idle(p)
    assert dropped(p) == 0


def test_counter_saturates_at_the_maximum(mk):
    assert MAX_DROPPED == 4294967295
    p = mk(Fake(False))
    p._dropped = MAX_DROPPED - 1
    send_one(p)
    assert dropped(p) == MAX_DROPPED
    send_one(p)
    assert dropped(p) == MAX_DROPPED
    p._transport.block, p._transport.release = True, threading.Event()
    p.offer(p.build_sample(1.0))
    assert p._transport.started.wait(1)
    p.offer(p.build_sample(1.0))  # busy rejection also saturates
    assert dropped(p) == MAX_DROPPED


# ---- D: exceptions never kill the sender or double count ----

def test_transport_exception_counts_once_and_the_next_sample_is_delivered(mk):
    t = Fake(RuntimeError("boom"))
    p = mk(t)
    send_one(p)
    assert dropped(p) == 1 and p._thread.is_alive()
    t.result = True
    send_one(p)
    assert dropped(p) == 0 and len(t.calls) == 2


def test_serialization_failure_counts_once_and_is_not_offered(mk, monkeypatch):
    t = Fake()
    p = mk(t)
    s = p.build_sample(1.0)
    p.offer({"dropped": 0, "bad": float("nan")})
    assert dropped(p) == 1
    monkeypatch.setattr(probe_mod.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(TypeError("x")))
    p.offer(s)
    monkeypatch.undo()
    assert dropped(p) == 2 and not t.calls and p._slot is None
    send_one(p)
    assert len(t.calls) == 1


def test_on_sample_observer_sees_the_sample_and_is_not_an_acknowledgement(mk):
    seen = []
    t = Fake(False)
    p = mk(t, on_sample=seen.append)
    send_one(p)
    assert len(seen) == 1 and seen[0]["seq"] == 0 and dropped(p) == 1  # only the sender changes dropped


def test_on_sample_exception_counts_once_does_not_offer_and_loop_survives(mk):
    t = Fake()
    mode = {"raise": True}

    def hook(sample):
        if mode["raise"]:
            raise RuntimeError("hook")

    p = mk(t, on_sample=hook)
    p.offer(p.build_sample(1.0))
    assert dropped(p) == 1 and not t.calls and p._slot is None
    mode["raise"] = False
    send_one(p)
    assert len(t.calls) == 1 and p._thread.is_alive() and dropped(p) == 0 and json.loads(t.calls[0][0])["dropped"] == 1


# ---- E: deadline with an injected clock ----

def clocked(ticks):
    it = iter(ticks)
    last = [ticks[-1]]

    def now():
        last[0] = next(it, last[0])
        return last[0]

    return now


def test_delivery_before_the_deadline_is_acknowledged_with_the_remaining_budget(mk):
    t = Fake()
    p = mk(t, monotonic=clocked([10.0, 10.05, 10.1]))
    p._dropped = 2
    send_one(p)
    assert t.calls[0][1] == pytest.approx(0.15) and dropped(p) == 0


def test_true_returned_after_the_deadline_is_not_acknowledged(mk):
    t = Fake(True)
    p = mk(t, monotonic=clocked([10.0, 10.0, 10.25]))
    send_one(p)
    assert t.calls[0][1] == pytest.approx(0.2) and dropped(p) == 1


def test_handoff_delay_beyond_the_deadline_skips_the_send(mk):
    t = Fake(True)
    p = mk(t, monotonic=clocked([10.0, 10.3]))
    send_one(p)
    assert not t.calls and dropped(p) == 1
