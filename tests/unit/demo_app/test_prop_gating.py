"""Property 20: destructive endpoints are routed only when the flag is exactly "1" (Task 4.13).

**Validates: Requirements 1.10, 1.11, 13.16, 15.6**

Every create_app call injects a recording exit_fn; the real os._exit is never reachable here.
The routing oracle (`flag == "1"`) is written independently of app internals. The negative
controls use deliberately broken, test-local FastAPI apps and prove the shared check helpers
reject them.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from coroner.demo_app.app import create_app
from coroner.demo_app.leak import LeakStore

FLAG = "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS"
DESTRUCTIVE = {("POST", "/leak"), ("POST", "/crash"), ("GET", "/error")}
DESTRUCTIVE_PATHS = {"/leak", "/crash", "/error"}


class FakeProbe:
    def __init__(self) -> None:
        self.config = SimpleNamespace(run_id="run-1")
        self.requests = 0
        self._lock = threading.Lock()

    def count_request(self) -> None:
        with self._lock:
            self.requests += 1

    @property
    def state(self) -> dict:
        return {"next_seq": 0, "dropped": 0}

    def start(self, loop=None) -> None:
        asyncio.get_running_loop()

    async def stop(self) -> None:
        pass


def oracle_enabled(flag) -> bool:
    return flag is not None and flag == "1"


def route_pairs(app) -> set[tuple[str, str]]:
    return {(m, r.path) for r in app.routes for m in getattr(r, "methods", None) or ()}


def check_gating(app, probe, exits, flag, leak_bytes: int = 8) -> None:
    """Shared assertions. Raises AssertionError on any deviation from Property 20."""
    expected = oracle_enabled(flag)
    pairs = route_pairs(app)
    store = app.state.leak_store
    made = 0
    with TestClient(app, raise_server_exceptions=False) as c:
        if expected:
            assert DESTRUCTIVE <= pairs, pairs
            assert {p for _, p in pairs} >= DESTRUCTIVE_PATHS
            # only the approved methods are registered for the three paths
            assert {(m, p) for m, p in pairs if p in DESTRUCTIVE_PATHS} == DESTRUCTIVE, pairs
            r = c.post(f"/leak?bytes={leak_bytes}")
            made += 1
            assert r.status_code == 200, r.text
            assert r.json() == {"retained_bytes_total": leak_bytes, "requested_bytes": leak_bytes}
            assert store.total == leak_bytes and store.chunk_count == 1
            assert store._chunks[0] == b"\xa5" * leak_bytes
            wrong = c.get("/leak")
            made += 1
            assert wrong.status_code in (404, 405), wrong.status_code
            assert c.get("/error").status_code == 500
            made += 1
            assert exits == []
            c.post("/crash")
            made += 1
            assert exits == [1]
        else:
            assert not (pairs & DESTRUCTIVE), pairs
            assert not ({p for _, p in pairs} & DESTRUCTIVE_PATHS), pairs
            for method, url in (("POST", "/leak?bytes=8"), ("POST", "/crash"), ("GET", "/error")):
                r = c.request(method, url)
                made += 1
                assert r.status_code == 404, (method, url, r.status_code)
            assert exits == []
            assert store.total == 0 and store.chunk_count == 0
        assert c.get("/health").status_code == 200
        made += 1
    assert probe.requests == made, (probe.requests, made)  # each request, 404s included, counted once


def build_real(flag):
    probe = FakeProbe()
    exits: list = []
    env = {} if flag is None else {FLAG: flag}
    app = create_app(env, probe=probe, exit_fn=lambda code: exits.append(code))
    return app, probe, exits


SPECIALS = ["", "0", "true", "True", " 1", "1 ", "01", "1\n", "\u0661", "\uff11", "yes", "2", "-1", "11"]
FLAGS = st.one_of(
    st.none(),
    st.sampled_from(SPECIALS),
    st.text(max_size=12),
    st.integers().map(str),
    st.just("1"),
    st.just("1"),
    st.just("1"),
)


def flag_class(flag) -> str:
    if flag is None:
        return "missing"
    if flag == "1":
        return "exact-1"
    if flag == "":
        return "empty"
    if flag.strip() == "1":
        return "whitespace-1"
    if flag.isdigit():
        return "digits"
    return "other"


@settings(max_examples=300, derandomize=True, database=None, deadline=None,
          suppress_health_check=list(HealthCheck))
@given(flag=FLAGS, n=st.integers(min_value=1, max_value=64))
@example(flag=None, n=8)
@example(flag="", n=8)
@example(flag="0", n=8)
@example(flag="true", n=8)
@example(flag="True", n=8)
@example(flag=" 1", n=8)
@example(flag="1 ", n=8)
@example(flag="01", n=8)
@example(flag="\u0661", n=8)
@example(flag="\uff11", n=8)
@example(flag="1", n=8)
def test_property_20_routed_only_when_flag_is_exactly_one(flag, n) -> None:
    event(f"{'enabled' if oracle_enabled(flag) else 'disabled'}")
    event(f"class={flag_class(flag)}")
    app, probe, exits = build_real(flag)
    check_gating(app, probe, exits, flag, n)


def test_enabled_get_leak_is_not_200_recorded() -> None:
    app, probe, exits = build_real("1")
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/leak").status_code == 405  # recorded framework behaviour
        assert c.post("/error").status_code == 405
    assert exits == []


# --- negative controls: deliberately broken local apps -------------------------------------------


def broken_app(flag, *, enable, disabled_status=404, exit_when_disabled=False, count_404=True):
    """Local fake create_app with one injected defect. Returns (app, probe, exits)."""
    probe = FakeProbe()
    exits: list = []
    app = FastAPI()
    store = LeakStore()
    app.state.leak_store = store

    @app.middleware("http")
    async def counting(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            probe.count_request()
            raise
        if count_404 or response.status_code != 404:
            probe.count_request()
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    if enable(flag):

        @app.post("/leak")
        def leak(bytes: int):
            return {"retained_bytes_total": store.retain(bytes), "requested_bytes": bytes}

        @app.post("/crash")
        def crash():
            exits.append(1)
            return {}

        @app.get("/error")
        def error():
            raise RuntimeError("x")

    else:

        # Routes stay unregistered; the defect is injected in a middleware instead, so the
        # status-code and exit_fn assertions are exercised on their own (not via route absence).
        if disabled_status != 404 or exit_when_disabled:

            @app.middleware("http")
            async def defect(request, call_next):
                if request.url.path in DESTRUCTIVE_PATHS:
                    if exit_when_disabled:
                        exits.append(1)
                    if disabled_status != 404:
                        probe.count_request()
                        return JSONResponse({}, status_code=disabled_status)
                return await call_next(request)

    return app, probe, exits


def test_control_sanity_correct_local_app_passes_helper() -> None:
    for flag in (None, "true", "1"):
        app, probe, exits = broken_app(flag, enable=lambda v: v == "1")
        check_gating(app, probe, exits, flag)


def test_control_enable_on_true_is_rejected() -> None:
    app, probe, exits = broken_app("true", enable=lambda v: v in ("1", "true"))
    with pytest.raises(AssertionError):
        check_gating(app, probe, exits, "true")


def test_control_routes_registered_when_flag_missing_is_rejected() -> None:
    app, probe, exits = broken_app(None, enable=lambda v: True)
    with pytest.raises(AssertionError):
        check_gating(app, probe, exits, None)


def test_control_403_instead_of_404_is_rejected() -> None:
    app, probe, exits = broken_app("0", enable=lambda v: v == "1", disabled_status=403)
    with pytest.raises(AssertionError):
        check_gating(app, probe, exits, "0")


def test_control_exit_fn_called_while_disabled_is_rejected() -> None:
    app, probe, exits = broken_app("0", enable=lambda v: v == "1", exit_when_disabled=True)
    with pytest.raises(AssertionError):
        check_gating(app, probe, exits, "0")


def test_control_uncounted_404_is_rejected() -> None:
    app, probe, exits = broken_app("0", enable=lambda v: v == "1", count_404=False)
    with pytest.raises(AssertionError):
        check_gating(app, probe, exits, "0")


def test_control_enabled_flag_one_not_routed_is_rejected() -> None:
    app, probe, exits = broken_app("1", enable=lambda v: False)
    with pytest.raises(AssertionError):
        check_gating(app, probe, exits, "1")
