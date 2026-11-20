"""Property 28, runner side, plus readiness examples (Task 6.12; Requirements 8.7, 8.8, 8.14, 11.10).

**Validates: Requirements 8.14, 11.10**

Property 28 (runner side): for any health response (any status code, any `storage_ok` value including absent or
non-boolean) the readiness check accepts it iff the status is 200 and the body is a JSON object with `storage_ok` exactly
`true`; any other 200 (including invalid JSON, empty and non-object bodies) aborts immediately with "Collector storage
is not healthy" and is never retried; non-200 and connection failures are retried to the 60 s deadline. Only fakes are used: no network, no real sleep.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coroner.runner import collector_client
from coroner.runner.collector_client import (
    STORAGE_UNHEALTHY_MESSAGE,
    CollectorClient,
    CollectorUnavailable,
    HealthResult,
    HttpResponse,
    check_ready,
)

T0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SERVER_TIME = "2025-01-01T12:00:00.000Z"


class FakeWorld:
    """Fake monotonic clock and sleep; the transport can also advance time per request."""

    def __init__(self, request_s: float = 0.0) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.request_s = request_s

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.now)


class ScriptedTransport:
    """Returns scripted responses in order, then repeats the last one. An Exception item is raised."""

    def __init__(self, world: FakeWorld, *script: HttpResponse | Exception) -> None:
        self.world = world
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, body: bytes | None, timeout_s: float) -> HttpResponse:
        assert timeout_s > 0
        self.calls.append((method, url))
        self.world.now += self.world.request_s
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def health_body(**fields: object) -> bytes:
    body: dict[str, object] = {"status": "ok", "durability": "crash", "server_time": SERVER_TIME, "uptime_s": 3}
    body.update(fields)
    return json.dumps(body).encode()


def run(*script: HttpResponse | Exception, request_s: float = 0.0):
    world = FakeWorld(request_s)
    transport = ScriptedTransport(world, *script)
    client = CollectorClient("http://127.0.0.1:8081", transport=transport, wall_clock=world.wall, timeout_s=2.0)
    outcome = check_ready(client, world.clock, world.sleep)
    return outcome, world, transport


# ---- Property 28 ----

_NOT_TRUE = st.one_of(
    st.just(False),
    st.none(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=8),
    st.just("true"),
    st.just(1),
    st.lists(st.booleans(), max_size=2),
    st.dictionaries(st.text(max_size=3), st.booleans(), max_size=2),
)
_ABSENT = object()


@settings(max_examples=200, deadline=None)
@given(
    status=st.integers(min_value=100, max_value=599),
    value=st.one_of(st.just(True), _NOT_TRUE, st.just(_ABSENT)),
)
def test_prop28_accepted_iff_200_and_storage_ok_exactly_true(status: int, value: object) -> None:
    fields: dict[str, object] = {} if value is _ABSENT else {"storage_ok": value}
    body = json.dumps({"status": "ok", "server_time": SERVER_TIME, **fields}).encode()
    outcome, world, transport = run(HttpResponse(status, body))
    expected_accept = status == 200 and value is True  # oracle: the generator knows the exact drawn value
    assert outcome.ready is expected_accept
    assert outcome.demo_app_may_start is expected_accept
    if expected_accept:
        assert outcome.exit_code == 0 and len(transport.calls) == 1
    elif status == 200:  # 200 with false/absent/null/1/"true": immediate abort with the storage message
        assert outcome.status == "collector_storage_unhealthy"
        assert outcome.message == STORAGE_UNHEALTHY_MESSAGE
        assert outcome.exit_code != 0
        assert len(transport.calls) == 1 and world.sleeps == []
    else:  # other statuses are retried to the deadline, never accepted and never reported as storage failure
        assert outcome.status == "collector_unhealthy"
        assert outcome.exit_code != 0
        assert not outcome.demo_app_may_start


def oracle_verdict(status: int, raw: bytes) -> str:
    """Independent oracle: 'ready' | 'abort' (immediate storage failure) | 'retry' (until the 60 s window)."""
    if status != 200:
        return "retry"
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError and JSONDecodeError (empty body too)
        return "abort"
    if isinstance(decoded, dict) and decoded.get("storage_ok") is True:
        return "ready"
    return "abort"


def assert_matches_oracle(status: int, raw: bytes) -> None:
    verdict = oracle_verdict(status, raw)
    outcome, world, transport = run(HttpResponse(status, raw))
    assert outcome.ready is (verdict == "ready")
    assert outcome.demo_app_may_start is (verdict == "ready")
    if verdict == "ready":
        assert outcome.status == "ready" and outcome.exit_code == 0
        assert len(transport.calls) == 1 and world.sleeps == []
    elif verdict == "abort":
        assert outcome.status == "collector_storage_unhealthy"
        assert outcome.message == STORAGE_UNHEALTHY_MESSAGE and outcome.exit_code == 1
        assert outcome.attempts == 1 and len(transport.calls) == 1
        assert world.sleeps == [] and world.now == 0.0
    else:
        assert outcome.status == "collector_unhealthy" and outcome.exit_code == 1
        assert outcome.attempts > 1 and world.now >= 60.0


_JSON_VALUES = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=6)),
    lambda children: st.one_of(st.lists(children, max_size=3), st.dictionaries(st.text(max_size=6), children, max_size=3)),
    max_leaves=6,
)
_BODIES = st.one_of(
    st.binary(max_size=40),  # mostly invalid JSON / invalid UTF-8
    st.just(b""),
    _JSON_VALUES.map(lambda v: json.dumps(v).encode()),  # valid JSON of every shape
    st.sampled_from([True, False, 1, "true", None, [], {}]).map(lambda v: json.dumps({"storage_ok": v}).encode()),
)


@settings(max_examples=150, deadline=None)
@given(status=st.sampled_from([100, 200, 200, 200, 204, 301, 404, 500, 503]), raw=_BODIES)
def test_prop28_arbitrary_bodies_match_independent_oracle(status: int, raw: bytes) -> None:
    assert_matches_oracle(status, raw)


# ---- Readiness examples ----

def test_200_true_proceeds_immediately() -> None:
    outcome, world, transport = run(HttpResponse(200, health_body(storage_ok=True)))
    assert outcome.status == "ready" and outcome.ready and outcome.demo_app_may_start and outcome.exit_code == 0
    assert transport.calls == [("GET", "http://127.0.0.1:8081/healthz")]
    assert outcome.health is not None and outcome.health.durability == "crash"
    assert outcome.clock_offset is not None and outcome.clock_offset.offset_ms == 0


def test_200_false_aborts_immediately() -> None:
    outcome, world, transport = run(HttpResponse(200, health_body(storage_ok=False, storage_error="disk full")))
    assert outcome.status == "collector_storage_unhealthy"
    assert outcome.message == "Collector storage is not healthy"
    assert outcome.exit_code != 0 and not outcome.demo_app_may_start
    assert len(transport.calls) == 1 and world.sleeps == []


def test_200_absent_aborts_immediately() -> None:
    outcome, world, transport = run(HttpResponse(200, health_body()))
    assert outcome.status == "collector_storage_unhealthy" and not outcome.demo_app_may_start
    assert len(transport.calls) == 1 and world.sleeps == []


@pytest.mark.parametrize("value", [1, "true", None, 0, "yes", [True]])
def test_200_non_boolean_storage_ok_aborts_immediately(value: object) -> None:
    outcome, world, transport = run(HttpResponse(200, health_body(storage_ok=value)))
    assert outcome.status == "collector_storage_unhealthy" and not outcome.demo_app_may_start
    assert len(transport.calls) == 1


def test_persistent_503_times_out_within_60s() -> None:
    outcome, world, transport = run(HttpResponse(503, b'{"error":"storage_failure"}'))
    assert outcome.status == "collector_unhealthy" and not outcome.demo_app_may_start and outcome.exit_code != 0
    assert 60.0 <= world.now < 61.0 + 1e-9
    assert outcome.waited_s == pytest.approx(world.now)
    assert len(transport.calls) > 1


def test_503_with_storage_ok_true_body_is_still_rejected() -> None:
    outcome, _, _ = run(HttpResponse(503, health_body(storage_ok=True)))
    assert outcome.status == "collector_unhealthy"


@pytest.mark.parametrize("error", [TimeoutError("timed out"), ConnectionRefusedError("refused"), CollectorUnavailable("down")])
def test_persistent_transport_errors_time_out(error: Exception) -> None:
    outcome, world, _ = run(error)
    assert outcome.status == "collector_unhealthy" and not outcome.demo_app_may_start
    assert outcome.health is not None and outcome.health.http_status is None
    assert world.now >= 60.0


def test_slow_requests_still_end_the_window_at_60s() -> None:
    outcome, world, transport = run(TimeoutError("timed out"), request_s=2.0)
    assert outcome.status == "collector_unhealthy"
    assert 60.0 <= world.now <= 63.0
    assert len(transport.calls) <= 25


MALFORMED_200_BODIES = [
    pytest.param(b"not json", id="malformed-json"),
    pytest.param(b'{"storage_ok": true', id="truncated-json"),
    pytest.param(b"\xff\xfe", id="invalid-utf8"),
    pytest.param(b"", id="empty"),
    pytest.param(b"null", id="null"),
    pytest.param(b"[]", id="empty-array"),
    pytest.param(b'[{"storage_ok": true}]', id="array-with-object"),
    pytest.param(b"[true]", id="array-of-true"),
    pytest.param(b"0", id="number-zero"),
    pytest.param(b"1", id="number-one"),
    pytest.param(b"1.5", id="number-float"),
    pytest.param(b'"true"', id="string-true"),
    pytest.param(b'""', id="string-empty"),
    pytest.param(b"true", id="boolean-true"),
    pytest.param(b"false", id="boolean-false"),
    pytest.param(b"{}", id="object-without-storage_ok"),
    pytest.param(b'{"status":"ok","server_time":"2025-01-01T12:00:00.000Z"}', id="health-object-without-storage_ok"),
]


@pytest.mark.parametrize("raw", MALFORMED_200_BODIES)
def test_200_malformed_or_non_object_body_aborts_immediately(raw: bytes) -> None:
    outcome, world, transport = run(HttpResponse(200, raw))
    assert outcome.status == "collector_storage_unhealthy"
    assert outcome.message == "Collector storage is not healthy"
    assert outcome.exit_code == 1
    assert outcome.demo_app_may_start is False and outcome.ready is False
    assert outcome.attempts == 1 and len(transport.calls) == 1
    assert world.sleeps == [] and world.now == 0.0  # no poll consumed
    assert_matches_oracle(200, raw)


@pytest.mark.parametrize("raw", [b"not json", b"", b"null", b"[]"])
def test_non_200_with_unusable_body_is_still_retried(raw: bytes) -> None:
    outcome, world, transport = run(HttpResponse(503, raw))
    assert outcome.status == "collector_unhealthy" and not outcome.demo_app_may_start
    assert len(transport.calls) > 1 and world.now >= 60.0


def test_malformed_200_after_transient_failure_aborts_on_that_attempt() -> None:
    outcome, world, transport = run(HttpResponse(503, b"{}"), HttpResponse(200, b"garbage"))
    assert outcome.status == "collector_storage_unhealthy" and outcome.attempts == 2
    assert len(transport.calls) == 2 and len(world.sleeps) == 1


def test_recovers_after_transient_failures() -> None:
    outcome, world, transport = run(
        ConnectionRefusedError("refused"), HttpResponse(503, b"{}"), HttpResponse(200, health_body(storage_ok=True))
    )
    assert outcome.ready and len(transport.calls) == 3 and len(world.sleeps) == 2


def test_storage_unhealthy_after_transient_failure_still_aborts_immediately() -> None:
    outcome, _, transport = run(HttpResponse(503, b"{}"), HttpResponse(200, health_body(storage_ok=False)))
    assert outcome.status == "collector_storage_unhealthy" and len(transport.calls) == 2


def test_invalid_server_time_does_not_change_readiness_but_is_reported() -> None:
    outcome, _, _ = run(HttpResponse(200, health_body(storage_ok=True, server_time="yesterday")))
    assert outcome.ready
    assert outcome.clock_offset is None and outcome.health is not None and outcome.health.clock_offset_error


def test_evidence_and_termination_requests_use_documented_paths() -> None:
    world = FakeWorld()
    transport = ScriptedTransport(world, HttpResponse(404, b'{"error":"not_found"}'))
    client = CollectorClient("http://127.0.0.1:8081/", transport=transport, wall_clock=world.wall)
    assert client.get_evidence("a-20250101T120000Z-9f3a1c2e").status == 404
    client.post_termination("a-20250101T120000Z-9f3a1c2e", {"k": 1})
    assert transport.calls == [
        ("GET", "http://127.0.0.1:8081/v1/runs/a-20250101T120000Z-9f3a1c2e/evidence"),
        ("POST", "http://127.0.0.1:8081/v1/runs/a-20250101T120000Z-9f3a1c2e/termination"),
    ]
    client.get_evidence("../x")
    assert transport.calls[-1][1].endswith("/v1/runs/..%2Fx/evidence")


def test_evidence_transport_failure_raises_collector_unavailable() -> None:
    world = FakeWorld()
    client = CollectorClient("http://h", transport=ScriptedTransport(world, TimeoutError("t")), wall_clock=world.wall)
    with pytest.raises(CollectorUnavailable):
        client.get_evidence("r")


# ---- Negative controls (fixtures only; they check the tests above can fail) ----

def _naive_accepts(status: int, value: object) -> bool:
    """A deliberately wrong readiness rule: truthiness instead of `is True`."""
    return status == 200 and bool(value)


def test_negative_control_truthiness_rule_is_detected_by_the_oracle() -> None:
    for value in (1, "true", [True]):
        assert _naive_accepts(200, value) is True  # the naive rule would accept
        outcome, _, _ = run(HttpResponse(200, health_body(storage_ok=value)))
        assert outcome.ready is False  # the real implementation, and so the property, rejects


def test_negative_control_retrying_bad_json_mutant_disagrees_with_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant: the old rule, where a 200 with an unusable body is retried instead of aborting."""
    monkeypatch.setattr(
        HealthResult,
        "storage_unhealthy",
        property(lambda self: self.http_status == 200 and self.body_valid and self.storage_ok is not True),
    )
    for raw in (b"not json", b"", b"null", b"[]", b"1", b'"x"', b"true"):
        outcome, world, _ = run(HttpResponse(200, raw))
        assert oracle_verdict(200, raw) == "abort"
        assert outcome.status == "collector_unhealthy" and world.now >= 60.0  # mutant retries
        with pytest.raises(AssertionError):
            assert_matches_oracle(200, raw)


def test_negative_control_truthy_storage_ok_mutant_disagrees_with_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant: accept any truthy storage_ok."""
    real = collector_client._classify

    def truthy(status: int | None, body: bytes | None):
        storage_ok, present, valid, obj = real(status, body)
        return (True if storage_ok else storage_ok), present, valid, obj

    monkeypatch.setattr(collector_client, "_classify", truthy)
    for value in (1, "true", [True], {"a": 1}):
        raw = json.dumps({"storage_ok": value}).encode()
        assert oracle_verdict(200, raw) == "abort"
        outcome, _, _ = run(HttpResponse(200, raw))
        assert outcome.ready is True  # mutant accepts
        with pytest.raises(AssertionError):
            assert_matches_oracle(200, raw)


def test_negative_control_status_only_rule_is_detected() -> None:
    def status_only(status: int, _value: object) -> bool:
        return status == 200

    outcome, _, _ = run(HttpResponse(200, health_body(storage_ok=False)))
    assert status_only(200, False) is True and outcome.ready is False
