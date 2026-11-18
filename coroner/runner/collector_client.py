"""Tiny stdlib HTTP client for the Collector (Task 6.11; Requirements 8.7, 8.8, 8.14, 9.8, 10.7, 11.10; design D14, D16).

Pieces:

- `CollectorClient`: `health()`, `get_evidence(run_id)` and `post_termination(run_id, payload)`. Every request has an
  explicit timeout. The transport, the wall clock and nothing else that touches the world are injectable, so unit tests
  need no network.
- `check_ready(client, ...)`: the runner preamble step 3. Accepts only HTTP 200 with `storage_ok is True`. A 200 whose
  `storage_ok` is anything else (false, absent, null, 1, "true") aborts at once with `collector_storage_unhealthy` and
  "Collector storage is not healthy"; so does a 200 whose body is invalid JSON, empty, or not a JSON object. Any other
  trouble (connection error, non-200 such as 503, timeout) is retried until the 60 s window ends, then
  `collector_unhealthy`. `ReadinessOutcome.demo_app_may_start` is False for both
  failures; the caller must not start the Demo_App in that case.
- `estimate_clock_offset_ms(host_before, host_after, server_time)`: midpoint method.

Clock offset sign convention (matches `classify_b_samples`, which compares `ts - clock_offset_ms`): the offset is
`server_time - host_midpoint` in milliseconds. A positive offset means the Collector (container) clock is AHEAD of the
host clock, and subtracting it from a container timestamp gives the equivalent host time. The uncertainty is half the
round trip, `(host_after - host_before) / 2`; `server_time` has millisecond truncation on top of that, which this
module does not add to the stated uncertainty. The 250 ms tolerance lives elsewhere and is not touched here.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from coroner.common.timeutil import format_utc_ms, parse_utc_ms

__all__ = [
    "READINESS_WINDOW_S",
    "STORAGE_UNHEALTHY_MESSAGE",
    "CollectorClient",
    "CollectorUnavailable",
    "ClockOffset",
    "ClockOffsetError",
    "HealthResult",
    "HttpResponse",
    "ReadinessOutcome",
    "check_ready",
    "estimate_clock_offset_ms",
    "urllib_transport",
]

READINESS_WINDOW_S = 60.0
READINESS_POLL_S = 1.0
DEFAULT_TIMEOUT_S = 5.0
STORAGE_UNHEALTHY_MESSAGE = "Collector storage is not healthy"
FAILURE_UNHEALTHY = "collector_unhealthy"
FAILURE_STORAGE_UNHEALTHY = "collector_storage_unhealthy"

# transport(method, url, body, timeout_s) -> HttpResponse; raises OSError-like errors when no response arrives.
Transport = Callable[[str, str, "bytes | None", float], "HttpResponse"]


class CollectorUnavailable(Exception):
    """No HTTP response arrived (connection error, timeout, reset)."""


class ClockOffsetError(ValueError):
    """The inputs to `estimate_clock_offset_ms` cannot give an estimate."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


def urllib_transport(opener: urllib.request.OpenerDirector | None = None) -> Transport:
    """Build a transport on urllib. The default opener ignores proxy settings (the Collector is on loopback)."""
    active = opener if opener is not None else urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def send(method: str, url: str, body: bytes | None, timeout_s: float) -> HttpResponse:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with active.open(request, timeout=timeout_s) as response:
                return HttpResponse(int(response.status), response.read())
        except urllib.error.HTTPError as exc:  # a real HTTP answer with an error status
            try:
                return HttpResponse(int(exc.code), exc.read())
            finally:
                exc.close()
        except (urllib.error.URLError, OSError, ValueError) as exc:  # includes socket.timeout and http.client errors
            raise CollectorUnavailable(f"{type(exc).__name__}: {exc}") from exc

    return send


@dataclass(frozen=True)
class ClockOffset:
    offset_ms: int  # server_time - host midpoint; positive means the Collector clock is ahead of the host clock
    uncertainty_ms: float  # half the round trip
    round_trip_ms: float
    window_start: datetime  # host time immediately before the request
    window_end: datetime  # host time immediately after the response
    server_time: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "clock_offset_ms": self.offset_ms,
            "uncertainty_ms": self.uncertainty_ms,
            "round_trip_ms": self.round_trip_ms,
            "window_start": format_utc_ms(self.window_start),
            "window_end": format_utc_ms(self.window_end),
            "server_time": format_utc_ms(self.server_time),
        }


def _aware(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ClockOffsetError(f"{name} must be a timezone-aware datetime, got {value!r}")
    return value.astimezone(timezone.utc)


def estimate_clock_offset_ms(host_before: datetime, host_after: datetime, server_time: str | datetime) -> ClockOffset:
    """Midpoint-method estimate. `server_time` is the Collector's `YYYY-MM-DDTHH:mm:ss.sssZ` string (or an aware datetime).

    Raises `ClockOffsetError` for naive host times, `host_after < host_before` (non-monotonic host clock) or an
    invalid `server_time`. The offset is rounded to the nearest millisecond (ties to even).
    """
    before = _aware("host_before", host_before)
    after = _aware("host_after", host_after)
    if after < before:
        raise ClockOffsetError(f"host_after {after.isoformat()} is earlier than host_before {before.isoformat()}")
    if isinstance(server_time, datetime):
        server = _aware("server_time", server_time)
    elif isinstance(server_time, str):
        try:
            server = parse_utc_ms(server_time)
        except ValueError as exc:
            raise ClockOffsetError(f"invalid server_time: {exc}") from exc
    else:
        raise ClockOffsetError(f"server_time must be a string, got {type(server_time).__name__}")
    rtt = after - before
    midpoint = before + rtt / 2
    delta = server - midpoint
    offset_us = delta // timedelta(microseconds=1)  # exact integer microseconds (floor)
    offset_ms = round(offset_us / 1000)
    rtt_ms = rtt / timedelta(milliseconds=1)
    return ClockOffset(offset_ms, rtt_ms / 2, rtt_ms, before, after, server)


@dataclass(frozen=True)
class HealthResult:
    """One `/healthz` observation. `ok` is the readiness verdict for this single response."""

    http_status: int | None  # None: no response arrived
    storage_ok: Any  # the raw decoded value; None when absent or the body was unusable
    storage_ok_present: bool
    ok: bool  # http_status == 200 and storage_ok is True
    checked_time: datetime
    latency_ms: float
    durability: str | None = None
    server_time: str | None = None
    body_valid: bool = False  # the body decoded to a JSON object
    error: str | None = None
    clock_offset: ClockOffset | None = None
    clock_offset_error: str | None = None

    @property
    def storage_unhealthy(self) -> bool:
        """HTTP 200 whose `storage_ok` is not exactly True: false, absent, null, 1, "true", or a body that is not
        a JSON object (invalid JSON, empty, null, array, number, string, boolean)."""
        return self.http_status == 200 and self.storage_ok is not True


def _classify(status: int | None, body: bytes | None) -> tuple[Any, bool, bool, dict[str, Any]]:
    """Return `(storage_ok, present, body_valid, obj)`."""
    if body is None:
        return None, False, False, {}
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, False, False, {}
    if not isinstance(obj, dict):
        return None, False, False, {}
    return obj.get("storage_ok"), "storage_ok" in obj, True, obj


class CollectorClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._transport: Transport = transport if transport is not None else urllib_transport()
        self._wall: Callable[[], datetime] = wall_clock if wall_clock is not None else (lambda: datetime.now(timezone.utc))

    def _send(self, method: str, path: str, body: bytes | None = None) -> HttpResponse:
        try:
            return self._transport(method, self.base_url + path, body, self.timeout_s)
        except CollectorUnavailable:
            raise
        except (OSError, ValueError) as exc:  # includes TimeoutError from an injected transport
            raise CollectorUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def health(self) -> HealthResult:
        """GET /healthz. Never raises: failures are reported in the result. Estimates the clock offset when possible."""
        before = self._wall()
        try:
            response = self._send("GET", "/healthz")
        except CollectorUnavailable as exc:
            after = self._wall()
            return HealthResult(None, None, False, False, after, _ms_between(before, after), error=str(exc))
        after = self._wall()
        storage_ok, present, valid, obj = _classify(response.status, response.body)
        ok = response.status == 200 and valid and storage_ok is True
        server_time = obj.get("server_time")
        durability = obj.get("durability")
        offset: ClockOffset | None = None
        offset_error: str | None = None
        if response.status == 200 and valid:
            try:
                offset = estimate_clock_offset_ms(before, after, server_time)  # type: ignore[arg-type]
            except ClockOffsetError as exc:
                offset_error = str(exc)
        error = None if valid or response.status != 200 else "unparseable health body"
        return HealthResult(
            response.status,
            storage_ok,
            present,
            ok,
            after,
            _ms_between(before, after),
            durability=durability if isinstance(durability, str) else None,
            server_time=server_time if isinstance(server_time, str) else None,
            body_valid=valid,
            error=error,
            clock_offset=offset,
            clock_offset_error=offset_error,
        )

    def get_evidence(self, run_id: str) -> HttpResponse:
        """GET /v1/runs/{run_id}/evidence. Any HTTP status is returned (404 means no evidence). No response: `CollectorUnavailable`."""
        return self._send("GET", f"/v1/runs/{quote(run_id, safe='')}/evidence")

    def post_termination(self, run_id: str, payload: dict[str, Any]) -> HttpResponse:
        """POST /v1/runs/{run_id}/termination with a JSON body. No response: `CollectorUnavailable`."""
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return self._send("POST", f"/v1/runs/{quote(run_id, safe='')}/termination", body)


def _ms_between(start: datetime, end: datetime) -> float:
    return (end - start) / timedelta(milliseconds=1)


@dataclass(frozen=True)
class ReadinessOutcome:
    status: str  # "ready" | "collector_unhealthy" | "collector_storage_unhealthy"
    message: str
    exit_code: int
    health: HealthResult | None  # the last observation (None if no attempt completed)
    attempts: int
    waited_s: float

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def demo_app_may_start(self) -> bool:
        """The caller must start the Demo_App only when this is True."""
        return self.status == "ready"

    @property
    def clock_offset(self) -> ClockOffset | None:
        return None if self.health is None else self.health.clock_offset


def check_ready(
    client: CollectorClient,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    *,
    window_s: float = READINESS_WINDOW_S,
    poll_s: float = READINESS_POLL_S,
) -> ReadinessOutcome:
    """Poll `/healthz` for up to `window_s` (60 s) on the monotonic `clock`; `sleep` is injected for tests.

    - HTTP 200 and `storage_ok is True`: `ready`.
    - HTTP 200 whose body is not a JSON object with `storage_ok` exactly True (malformed, empty, non-object, or
      `storage_ok` false/absent/non-boolean): immediate `collector_storage_unhealthy`, no retry.
    - Anything else (no response, non-200): retry; at the deadline `collector_unhealthy`.
    """
    start = clock()
    deadline = start + window_s
    attempts = 0
    last: HealthResult | None = None
    while True:
        last = client.health()
        attempts += 1
        now = clock()
        if last.ok:
            return ReadinessOutcome("ready", "Collector is ready", 0, last, attempts, now - start)
        if last.storage_unhealthy:
            return ReadinessOutcome(
                FAILURE_STORAGE_UNHEALTHY, STORAGE_UNHEALTHY_MESSAGE, 1, last, attempts, now - start
            )
        remaining = deadline - now
        if remaining <= 0:
            break
        sleep(min(poll_s, remaining))
        if clock() >= deadline:
            break
    waited = clock() - start
    return ReadinessOutcome(
        FAILURE_UNHEALTHY, f"Collector did not become healthy within {window_s:g} s", 1, last, attempts, waited
    )
