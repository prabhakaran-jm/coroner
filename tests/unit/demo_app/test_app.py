"""Demo_App /health, /crash, request counting, wording and lifecycle (Task 4.14; Requirements 13.15, 15).

Every create_app call passes a recording exit_fn (the real os._exit is unreachable). The assertion
helpers are also run against deliberately broken local fake apps (negative controls).

Wording scan rule (`crash_wording_violations`): in text that describes the crash endpoint, the phrase
"unhandled exception" is never allowed, and the word "exception" is allowed only when directly
negated ("not an exception", "no exception", "not ... exception" within the same clause), because
the crash is a controlled os._exit(1), not an exception. The scanned text is the crash handler's own
source (docstring + comments), the app.py module-docstring sentences that mention /crash, and the
sentences of the Task 4.11 section of docs/m0-results/m0.4-probe-demo.md that mention /crash. The /error
handler and sentences about /error are not scanned (they legitimately say "exception").
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coroner.demo_app import app as app_module
from coroner.demo_app.app import create_app

FLAG = "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS"
DOC = Path(__file__).resolve().parents[3] / "docs" / "m0-results" / "m0.4-probe-demo.md"


class FakeProbe:
    def __init__(self, run_id: str = "run-414", next_seq: int = 5, dropped: int = 2) -> None:
        self.config = SimpleNamespace(run_id=run_id)
        self.requests = 0
        self.starts = 0
        self.stops = 0
        self.next_seq = next_seq
        self.dropped = dropped
        self._lock = threading.Lock()

    def count_request(self) -> None:
        with self._lock:
            self.requests += 1

    @property
    def state(self) -> dict:
        return {"next_seq": self.next_seq, "dropped": self.dropped}

    def start(self, loop=None) -> None:
        asyncio.get_running_loop()
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1  # nothing the tests rely on beyond counting


def make(env: dict, probe: FakeProbe | None = None):
    exits: list = []
    probe = probe or FakeProbe()
    app = create_app(env, probe=probe, exit_fn=lambda *a: exits.append(a))
    return app, probe, exits


# ---- assertion helpers (also exercised by negative controls) --------------------------


def check_health(client: TestClient, probe: FakeProbe) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "pid", "run_id", "probe"}, set(body)
    assert set(body["probe"]) == {"next_seq", "dropped"}
    assert body["status"] == "ok"
    assert body["pid"] == os.getpid()
    assert body["run_id"] == probe.config.run_id
    assert body["probe"] == probe.state


def check_counts_404(client: TestClient, probe: FakeProbe) -> None:
    before = probe.requests
    assert client.get("/no-such-path").status_code == 404
    assert probe.requests == before + 1, "404 was not counted"


def check_crash(client: TestClient, exits: list) -> None:
    client.post("/crash")
    assert exits == [(1,)], exits


def check_start_once(probe: FakeProbe) -> None:
    assert probe.starts == 1, f"probe.start called {probe.starts} times"


# ---- /health ---------------------------------------------------------------------------


def test_health_exact_shape_values_and_one_count_per_call() -> None:
    app, probe, _ = make({})
    with TestClient(app) as c:
        check_health(c, probe)
        assert probe.requests == 1
        probe.next_seq, probe.dropped = 41, 9
        check_health(c, probe)  # follows the probe's live state
        assert probe.requests == 2


def test_health_run_id_comes_from_probe_config() -> None:
    app, probe, _ = make({}, FakeProbe(run_id="other-run.7"))
    with TestClient(app) as c:
        assert c.get("/health").json()["run_id"] == "other-run.7"


# ---- /crash ----------------------------------------------------------------------------


def test_crash_calls_exit_fn_once_with_1_and_nothing_else() -> None:
    app, probe, exits = make({FLAG: "1"})
    with TestClient(app) as c:  # no SystemExit may escape; the process is still running
        r = c.post("/crash")
        assert exits == [(1,)]
        assert r.status_code == 200 and r.json() == {"status": "exit_fn_returned"}
        assert probe.stops == 0 and probe.starts == 1  # probe untouched
        assert app.state.leak_store.total == 0 and app.state.leak_store.chunk_count == 0
        assert probe.requests == 1  # only the middleware count
        assert c.get("/health").status_code == 200
    assert exits == [(1,)]  # nothing else called it, including shutdown


def test_crash_does_not_raise_system_exit_or_use_os_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(code):
        raise AssertionError("real os._exit reached")

    monkeypatch.setattr(os, "_exit", forbidden)  # default arg was bound at def time; injected fn is used
    app, _, exits = make({FLAG: "1"})
    with TestClient(app) as c:
        c.post("/crash")
    assert exits == [(1,)]


# ---- request counting ------------------------------------------------------------------


def test_counting_includes_404_for_unknown_path_and_flag_off_routes() -> None:
    app, probe, exits = make({})
    with TestClient(app) as c:
        check_counts_404(c, probe)
        for method, url in (("POST", "/leak?bytes=1"), ("POST", "/crash"), ("GET", "/error")):
            before = probe.requests
            assert c.request(method, url).status_code == 404
            assert probe.requests == before + 1
    assert exits == []


# ---- wording scan ----------------------------------------------------------------------

_NEGATION = re.compile(r"\b(?:not|no|never|neither|nor|without)\b[^.;:]*$")


def crash_wording_violations(text: str) -> list[str]:
    """Violations in text describing /crash: 'unhandled exception', or non-negated 'exception'."""
    found: list[str] = []
    lowered = text.lower()
    if "unhandled exception" in lowered:
        found.append("unhandled exception")
    for m in re.finditer(r"exception", lowered):
        before = lowered[: m.start()]
        clause = re.split(r"[.;:\n]", before)[-1]
        if not _NEGATION.search(clause + " "):
            found.append(lowered[max(0, m.start() - 30): m.end() + 10])
    return found


def sentences_mentioning(text: str, needle: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z`*|#])|\n", text)
    return [p for p in parts if needle in p]


def crash_endpoint():
    app, _, _ = make({FLAG: "1"})
    return next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/crash")


def task_411_section() -> str:
    """Task 4.11 section with backticks removed and bold run-in labels (e.g. the
    "Crash vs ordinary exception." label, a comparison heading, not a description) stripped."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index("## Task 4.11")
    end = text.index("\n## Task ", start + 1)
    return re.sub(r"\*\*[^*\n]*\*\*", "", text[start:end].replace("`", ""))


def test_crash_handler_text_never_calls_the_crash_an_exception() -> None:
    handler = inspect.getsource(crash_endpoint())
    assert "exit_fn(1)" in handler
    assert crash_wording_violations(handler) == []
    module_doc = inspect.getdoc(app_module) or ""
    module_sentences = sentences_mentioning(module_doc, "/crash")
    assert module_sentences
    assert crash_wording_violations("\n".join(module_sentences)) == []


def test_crash_context_says_controlled_fatal_exit_via_os_exit() -> None:
    needle = "controlled fatal process exit via os._exit(1)"
    context = (inspect.getsource(crash_endpoint()) + (inspect.getdoc(app_module) or "")).lower()
    assert needle in context
    assert needle in task_411_section().lower()


def test_doc_sentences_about_crash_never_say_exception_for_it() -> None:
    sentences = sentences_mentioning(task_411_section(), "/crash")
    assert sentences, "no sentence mentions /crash"
    for sentence in sentences:  # each sentence on its own so clauses never leak across sentences
        assert crash_wording_violations(sentence) == [], sentence


def test_error_handler_legitimately_says_exception_so_it_is_not_scanned() -> None:
    app, _, _ = make({FLAG: "1"})
    error = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/error")
    assert app_module.ERROR_MESSAGE.startswith("ordinary exception")
    assert inspect.getsource(error)  # exists; deliberately excluded from the scan


@pytest.mark.parametrize(
    "bad",
    [
        "The crash endpoint raises an unhandled exception.",
        "POST /crash is an exception.",
        "/crash throws an exception and the process dies",
        "Unhandled Exception in crash",
    ],
)
def test_negative_control_wording_scan_rejects_bad_text(bad: str) -> None:
    assert crash_wording_violations(bad) != []


@pytest.mark.parametrize(
    "good",
    ["not an exception, not a 500", "so no exception is raised", "calls exit_fn(1) only"],
)
def test_wording_scan_accepts_negated_or_absent_exception(good: str) -> None:
    assert crash_wording_violations(good) == []


# ---- lifecycle -------------------------------------------------------------------------


def test_probe_start_once_requests_do_not_restart_stop_awaited_once() -> None:
    app, probe, _ = make({FLAG: "1"})
    assert probe.starts == 0
    with TestClient(app, raise_server_exceptions=False) as c:
        check_start_once(probe)
        for _ in range(3):
            c.get("/health")
        c.get("/nope")
        c.post("/crash")
        c.get("/error")
        check_start_once(probe)
        assert probe.stops == 0
    check_start_once(probe)
    assert probe.stops == 1


# ---- negative controls: broken local fake apps ----------------------------------------


def make_fake_app(probe: FakeProbe, exits: list, *, health_has_probe: bool = True, count_404: bool = True,
                  crash_code: int = 1, start_per_request: bool = False) -> FastAPI:
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
        if start_per_request:
            probe.start()
        response = await call_next(request)
        if count_404 or response.status_code != 404:
            probe.count_request()
        return response

    @app.get("/health")
    def health() -> dict:
        body = {"status": "ok", "pid": os.getpid(), "run_id": probe.config.run_id}
        if health_has_probe:
            body["probe"] = probe.state
        return body

    @app.post("/crash")
    def crash() -> dict:
        exits.append((crash_code,))
        return {"status": "exit_fn_returned"}

    return app


def run_fake(**kw):
    probe, exits = FakeProbe(), []
    return make_fake_app(probe, exits, **kw), probe, exits


def test_control_baseline_fake_app_passes_every_helper() -> None:
    app, probe, exits = run_fake()
    with TestClient(app) as c:
        check_health(c, probe)
        check_counts_404(c, probe)
        check_crash(c, exits)
        c.get("/health")
        check_start_once(probe)


def test_negative_control_health_state_missing() -> None:
    app, probe, _ = run_fake(health_has_probe=False)
    with TestClient(app) as c, pytest.raises(AssertionError):
        check_health(c, probe)


def test_negative_control_404_not_counted() -> None:
    app, probe, _ = run_fake(count_404=False)
    with TestClient(app) as c, pytest.raises(AssertionError, match="404 was not counted"):
        check_counts_404(c, probe)


def test_negative_control_crash_wrong_code() -> None:
    app, _, exits = run_fake(crash_code=0)
    with TestClient(app) as c, pytest.raises(AssertionError):
        check_crash(c, exits)


def test_negative_control_probe_start_per_request() -> None:
    app, probe, _ = run_fake(start_per_request=True)
    with TestClient(app) as c:
        c.get("/health")
        c.get("/health")
        with pytest.raises(AssertionError, match="probe.start called"):
            check_start_once(probe)
