"""Smoke tests for Demo_App app.py and main.py (Task 4.11).

Every /crash test injects a recording exit_fn. The real os._exit is never reachable from here.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient

from coroner.demo_app import main as main_module
from coroner.demo_app.app import MAX_LEAK_BYTES, create_app
from coroner.probe import Probe, ProbeConfig

FLAG = "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS"
DESTRUCTIVE = (("POST", "/leak?bytes=1"), ("POST", "/crash"), ("GET", "/error"))


class FakeProbe:
    def __init__(self, run_id: str = "run-1") -> None:
        self.config = SimpleNamespace(run_id=run_id)
        self.requests = 0
        self.starts = 0
        self.stops = 0
        self.next_seq = 7
        self.dropped = 3
        self._lock = threading.Lock()

    def count_request(self) -> None:
        with self._lock:
            self.requests += 1

    @property
    def state(self) -> dict:
        return {"next_seq": self.next_seq, "dropped": self.dropped}

    def start(self, loop=None) -> None:
        asyncio.get_running_loop()  # must be called on the running loop
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


def make(env: dict, probe=None):
    exits: list = []
    probe = probe or FakeProbe()
    return create_app(env, probe=probe, exit_fn=lambda code: exits.append(code)), probe, exits


# A. health -------------------------------------------------------------------------


def test_health_body_pid_run_id_probe_state_and_counting() -> None:
    app, probe, _ = make({})
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "pid": os.getpid(),
        "run_id": "run-1",
        "probe": {"next_seq": 7, "dropped": 3},
    }
    assert probe.requests == 1


# B. gating -------------------------------------------------------------------------


def test_flag_one_registers_all_three() -> None:
    app, _, exits = make({FLAG: "1"})
    paths = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ())}
    assert {("POST", "/leak"), ("POST", "/crash"), ("GET", "/error")} <= paths
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.post("/leak?bytes=1").status_code == 200
        assert c.get("/error").status_code == 500
        assert c.post("/crash").status_code != 404
    assert exits == [1]


@pytest.mark.parametrize("value", [None, "", "0", "true", "True", " 1", "1 ", "yes", "2", "01", "١"])
def test_any_other_flag_value_leaves_destructive_routes_unregistered(value) -> None:
    env = {} if value is None else {FLAG: value}
    app, probe, exits = make(env)
    with TestClient(app) as c:
        for method, url in DESTRUCTIVE:
            assert c.request(method, url).status_code == 404, (value, url)
        assert c.get("/health").status_code == 200
    assert exits == []
    assert app.state.leak_store.total == 0
    assert probe.requests == 4  # the 404s were counted too (check 10)


# C. leak ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["", "?bytes=", "?bytes=0", "?bytes=-1", "?bytes=abc", "?bytes=1.5", "?bytes=true", "?bytes=1e3",
     "?bytes=%205", "?bytes=5%20", "?bytes=%2B5", "?bytes=5.0", "?bytes=1_000", "?bytes=104857601",
     "?bytes=9999999999999", "?other=5"],
)
def test_leak_rejects_invalid_values_without_calling_retain(query: str) -> None:
    app, _, _ = make({FLAG: "1"})
    store = app.state.leak_store
    calls: list = []
    store.retain = lambda n: calls.append(n)  # spy; must never run
    with TestClient(app) as c:
        r = c.post("/leak" + query)
    assert r.status_code == 422, r.text
    assert calls == [] and store.total == 0


def test_leak_valid_values_are_cumulative_with_exact_body() -> None:
    app, _, _ = make({FLAG: "1"})
    with TestClient(app) as c:
        assert c.post("/leak?bytes=1").json() == {"retained_bytes_total": 1, "requested_bytes": 1}
        assert c.post("/leak?bytes=1024").json() == {"retained_bytes_total": 1025, "requested_bytes": 1024}
        assert c.post("/leak?bytes=007").json() == {"retained_bytes_total": 1032, "requested_bytes": 7}
    assert app.state.leak_store.total == 1032 and app.state.leak_store.chunk_count == 3


def test_leak_upper_bound_is_exactly_104857600() -> None:
    assert MAX_LEAK_BYTES == 104_857_600
    app, _, _ = make({FLAG: "1"})
    store = app.state.leak_store
    store._allocate = lambda n: b"x"  # never allocate 100 MiB; only the range check is under test
    store.retain = lambda n: n
    with TestClient(app) as c:
        r = c.post("/leak?bytes=104857600")
    assert r.status_code == 200 and r.json() == {"retained_bytes_total": 104857600, "requested_bytes": 104857600}


def test_leak_memory_error_is_503_and_keeps_earlier_chunks() -> None:
    app, _, _ = make({FLAG: "1"})
    store = app.state.leak_store

    def boom(n: int) -> bytes:
        raise MemoryError

    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.post("/leak?bytes=10").status_code == 200
        store._allocate = boom
        r = c.post("/leak?bytes=99")
        assert r.status_code == 503
        assert r.json() == {"error": "allocation_failed", "retained_bytes_total": 10}
        del store._allocate
        assert c.post("/leak?bytes=5").json()["retained_bytes_total"] == 15
    assert store.chunk_count == 2


def test_destructive_handlers_are_sync_and_there_is_no_release_route() -> None:
    app, _, _ = make({FLAG: "1"})
    routes = {r.path: r for r in app.routes if hasattr(r, "endpoint")}
    assert not asyncio.iscoroutinefunction(routes["/leak"].endpoint)
    assert {p for p in routes} >= {"/health", "/leak", "/crash", "/error"}
    assert not any("reset" in p or "release" in p for p in routes)


# D. crash --------------------------------------------------------------------------


def test_crash_calls_injected_exit_fn_once_with_1() -> None:
    app, _, exits = make({FLAG: "1"})
    with TestClient(app) as c:
        c.post("/crash")
    assert exits == [1]


def test_create_app_default_exit_fn_is_os_exit() -> None:
    assert inspect.signature(create_app).parameters["exit_fn"].default is os._exit


# E. ordinary exception -------------------------------------------------------------


def test_error_is_500_and_process_keeps_serving() -> None:
    app, probe, exits = make({FLAG: "1"})
    with TestClient(app, raise_server_exceptions=False) as c:
        pid = c.get("/health").json()["pid"]
        before = probe.requests
        assert c.get("/error").status_code == 500
        r = c.get("/health")
        assert r.status_code == 200 and r.json()["pid"] == pid
        assert probe.requests == before + 2
        assert probe.stops == 0
    assert exits == []


def test_error_raises_the_exact_runtime_error_when_not_suppressed() -> None:
    app, _, _ = make({FLAG: "1"})
    with TestClient(app) as c:
        with pytest.raises(RuntimeError, match="^ordinary exception for Experiment non-termination test$"):
            c.get("/error")


# F. lifecycle ----------------------------------------------------------------------


def test_probe_started_once_on_enter_and_stopped_once_on_exit() -> None:
    app, probe, _ = make({FLAG: "1"})
    assert probe.starts == 0
    with TestClient(app) as c:
        assert probe.starts == 1 and probe.stops == 0
        for _ in range(3):
            c.get("/health")
        assert probe.starts == 1
    assert probe.starts == 1 and probe.stops == 1


def test_create_app_does_not_start_probe_or_touch_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG, "1")
    app, probe, _ = make({})
    assert probe.starts == 0
    assert not any(getattr(r, "path", "") == "/crash" for r in app.routes)


def test_real_probe_with_noop_transport_integration() -> None:
    sent: list = []

    class Transport:
        def send(self, body: bytes, timeout_s: float) -> bool:
            sent.append(body)
            return True

    probe = Probe(ProbeConfig(run_id="real-1", collector_url="http://unused.invalid", interval_ms=100), transport=Transport())
    app = create_app({}, probe=probe)
    with TestClient(app) as c:
        body = c.get("/health").json()
    assert body["run_id"] == "real-1" and body["probe"]["next_seq"] >= 0
    assert probe._task is None  # stopped by lifespan shutdown


# G. main ---------------------------------------------------------------------------


def test_main_builds_app_from_supplied_env_and_runs_single_worker() -> None:
    calls: list = []
    env = {"CORONER_RUN_ID": "main-run", FLAG: "1"}
    main_module.main(env, run=lambda app, **kw: calls.append((app, kw)))
    (app, kw), = calls
    assert kw == {"host": "0.0.0.0", "port": 8080, "workers": 1}
    paths = {r.path for r in app.routes}
    assert "/crash" in paths
    with TestClient(app, raise_server_exceptions=False) as c:
        body = c.get("/health").json()
    assert body["run_id"] == "main-run"


def test_main_invalid_explicit_run_id_raises_and_does_not_run() -> None:
    calls: list = []
    with pytest.raises(ValueError, match="CORONER_RUN_ID"):
        main_module.main({"CORONER_RUN_ID": "bad id!"}, run=lambda *a, **k: calls.append(1))
    assert calls == []


def test_importing_main_does_not_call_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    def forbidden(*a, **k):
        raise AssertionError("uvicorn.run called at import")

    monkeypatch.setattr(uvicorn, "run", forbidden)
    importlib.reload(main_module)
    assert callable(main_module.main)
