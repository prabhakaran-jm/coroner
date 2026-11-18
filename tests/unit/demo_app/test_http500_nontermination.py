"""GET /error is an ordinary HTTP 500 and the process keeps running (Requirements 13.15, 15; Task 4.14).

Design (deterministic, no real-time sleeps):
- The app runs through its normal lifespan with a REAL Probe: injected fixed readers, a recording
  fake Transport (no network), and the maximum interval (60 s). The sampler loop therefore never
  ticks while the test runs, so the ONLY Samples are the two this test builds and offers by hand
  (Sample A before /error, Sample B after). That makes "seq advances by exactly 1" exact.
- After offering each Sample the test waits (bounded, 2 s) until the fake Transport has seen it,
  so the one-slot sender is idle before the next offer and nothing is dropped.
- Every create_app call passes a recording exit_fn; the real os._exit is unreachable.
- The assertion helper `check_continuity` is also run against deliberately broken local fakes
  (negative controls) and must raise AssertionError for each.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coroner.demo_app.app import ERROR_MESSAGE, FLAG_NAME, create_app
from coroner.probe import Probe, ProbeConfig

SENDER_NAME = "coroner-probe-sender"


class RecordingTransport:
    """Fake Transport: records every body, returns True, never touches the network."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self._changed = threading.Event()

    def send(self, body: bytes, timeout_s: float) -> bool:
        self.bodies.append(body)
        self._changed.set()
        return True

    def wait_for(self, count: int, timeout: float = 2.0) -> bool:
        """Bounded wait (no unconditional sleep) until `count` bodies have been delivered."""
        import time

        end = time.monotonic() + timeout
        while len(self.bodies) < count:
            rest = end - time.monotonic()
            if rest <= 0:
                return False
            self._changed.wait(rest)
            self._changed.clear()
        return True


def make_probe(transport, samples: list, *, pid_reader=os.getpid, cls=Probe) -> Probe:
    return cls(
        ProbeConfig(run_id="http500-run", collector_url="http://unused.invalid", interval_ms=60000),
        transport=transport,
        rss_reader=lambda: 123_456_789,
        pid_reader=pid_reader,
        container_id_reader=lambda: ("a" * 64, "mountinfo"),
        wall_clock=lambda: datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        on_sample=samples.append,
    )


def take_sample(probe: Probe, transport: RecordingTransport, delivered: int) -> dict:
    sample = probe.build_sample(0.0)
    probe.offer(sample)
    assert transport.wait_for(delivered), "fake transport never received the Sample"
    return sample


def sender_alive(probe: Probe) -> bool:
    thread = getattr(probe, "_thread", None)
    task = getattr(probe, "_task", None)
    return bool(thread and thread.is_alive() and task is not None and not task.done())


def run_scenario(app: FastAPI, probe: Probe, transport: RecordingTransport) -> dict:
    """health -> /error -> health with Sample A before and Sample B after; returns observations."""
    with TestClient(app, raise_server_exceptions=False) as client:
        a = take_sample(probe, transport, 1)
        h1 = client.get("/health")
        err = client.get("/error")
        h2 = client.get("/health")
        b = take_sample(probe, transport, 2)
        return {
            "statuses": (h1.status_code, err.status_code, h2.status_code),
            "pids": (h1.json()["pid"], h2.json()["pid"]),
            "a": a,
            "b": b,
            "active": sender_alive(probe),
            "expected_requests": 3,
        }


def check_continuity(obs: dict) -> None:
    assert obs["statuses"] == (200, 500, 200), obs["statuses"]
    p1, p2 = obs["pids"]
    assert p1 == p2 == os.getpid(), obs["pids"]
    a, b = obs["a"], obs["b"]
    assert a["pid"] == b["pid"] == os.getpid(), (a["pid"], b["pid"])
    assert b["seq"] == a["seq"] + 1, (a["seq"], b["seq"])
    assert b["request_count"] - a["request_count"] == obs["expected_requests"]
    assert obs["active"] is True, "probe/sender is no longer active"


# ---- real application -----------------------------------------------------------------


def build_real():
    samples: list = []
    transport = RecordingTransport()
    probe = make_probe(transport, samples)
    exits: list = []
    app = create_app({FLAG_NAME: "1"}, probe=probe, exit_fn=lambda *a: exits.append(a))
    return app, probe, transport, samples, exits


def test_error_500_keeps_same_process_and_samples_continue() -> None:
    app, probe, transport, samples, exits = build_real()
    pid_before = os.getpid()
    sender_before = sum(t.name == SENDER_NAME for t in threading.enumerate())
    assert sender_before == 0
    obs = run_scenario(app, probe, transport)
    check_continuity(obs)
    # Evidence of the exact sequence (recorded for the report).
    print("HTTP500-EVIDENCE", obs["statuses"], obs["pids"], obs["a"]["pid"], obs["b"]["pid"],
          obs["a"]["seq"], obs["b"]["seq"])
    assert (obs["a"]["seq"], obs["b"]["seq"]) == (0, 1)
    assert os.getpid() == pid_before
    assert exits == []  # /error never reaches exit_fn
    # Exactly the two hand-built Samples exist: the sampler loop did not tick (60 s interval).
    assert [s["seq"] for s in samples] == [0, 1]
    # The transport got exactly those two bodies, with the same pid and consecutive seq.
    decoded = [json.loads(b) for b in transport.bodies]
    assert [d["seq"] for d in decoded] == [0, 1]
    assert {d["pid"] for d in decoded} == {os.getpid()}
    assert all(d["dropped"] == 0 for d in decoded)
    # After the context exited the lifespan stopped the probe (shutdown, not the 500, stopped it).
    assert probe._task is None


def test_probe_state_and_health_reflect_continuity_through_the_500() -> None:
    app, probe, transport, _, _ = build_real()
    with TestClient(app, raise_server_exceptions=False) as client:
        take_sample(probe, transport, 1)
        assert client.get("/health").json()["probe"] == {"next_seq": 1, "dropped": 0}
        assert client.get("/error").status_code == 500
        assert sender_alive(probe)
        assert sum(t.name == SENDER_NAME and t.is_alive() for t in threading.enumerate()) == 1
        health = client.get("/health").json()
        assert health["pid"] == os.getpid()
        assert health["probe"] == {"next_seq": 1, "dropped": 0}  # the 500 produced/dropped nothing
        assert probe.state["next_seq"] == 1


def test_error_raises_runtime_error_inside_a_still_running_process() -> None:
    """With exceptions NOT suppressed the handler's ordinary exception surfaces; it is not an exit."""
    app, probe, transport, _, exits = build_real()
    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match=ERROR_MESSAGE):
            client.get("/error")
        assert client.get("/health").status_code == 200
    assert exits == []


def test_no_external_http_only_fake_transport() -> None:
    app, probe, transport, _, _ = build_real()
    assert probe._transport is transport
    run_scenario(app, probe, transport)
    assert len(transport.bodies) == 2


# ---- negative controls: deliberately broken local fakes ---------------------------------


def make_fake_app(probe: Probe, *, error_status: int = 500, stop_on_error: bool = False,
                  health_pid_shift_after_error: bool = False) -> FastAPI:
    state = {"errored": False}

    @asynccontextmanager
    async def lifespan(_app):
        probe.start()
        try:
            yield
        finally:
            await probe.stop()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def count(request, call_next):
        probe.count_request()
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        shift = 1 if (health_pid_shift_after_error and state["errored"]) else 0
        return {"status": "ok", "pid": os.getpid() + shift}

    @app.get("/error")
    async def error():
        state["errored"] = True
        if stop_on_error:
            await probe.stop()  # a broken error endpoint that kills sampling
        if error_status == 500:
            raise RuntimeError(ERROR_MESSAGE)
        return {"status": "fine"}

    return app


class StuckSeqProbe(Probe):
    def build_sample(self, loop_delay_ms, loop_delay_reason=None):
        sample = super().build_sample(loop_delay_ms, loop_delay_reason)
        sample["seq"] = 0  # seq never advances in what is observed
        return sample


def broken_obs(**kwargs) -> dict:
    samples: list = []
    transport = RecordingTransport()
    pid_reader = kwargs.pop("pid_reader", os.getpid)
    cls = kwargs.pop("cls", Probe)
    probe = make_probe(transport, samples, pid_reader=pid_reader, cls=cls)
    return run_scenario(make_fake_app(probe, **kwargs), probe, transport)


def test_control_baseline_fake_app_passes_the_helper() -> None:
    check_continuity(broken_obs())  # the fake is only broken where a control says so


def test_negative_control_error_returns_200() -> None:
    with pytest.raises(AssertionError):
        check_continuity(broken_obs(error_status=200))


def test_negative_control_error_stops_sampling() -> None:
    # Rejected either by the helper's "no longer active" check or because the stopped sender
    # can never deliver Sample B (take_sample's bounded wait); both are AssertionError.
    with pytest.raises(AssertionError):
        check_continuity(broken_obs(stop_on_error=True))


def test_negative_control_health_pid_changes_across_the_500() -> None:
    with pytest.raises(AssertionError):
        check_continuity(broken_obs(health_pid_shift_after_error=True))


def test_negative_control_sample_pid_changes_across_the_500() -> None:
    calls = {"n": 0}

    def drifting() -> int:
        calls["n"] += 1
        return os.getpid() + (1 if calls["n"] > 1 else 0)

    with pytest.raises(AssertionError):
        check_continuity(broken_obs(pid_reader=drifting))


def test_negative_control_seq_stops_advancing() -> None:
    with pytest.raises(AssertionError):
        check_continuity(broken_obs(cls=StuckSeqProbe))


def test_negative_control_request_count_not_increased() -> None:
    obs = broken_obs()
    obs["expected_requests"] = 4  # the helper must notice a count mismatch
    with pytest.raises(AssertionError):
        check_continuity(obs)
