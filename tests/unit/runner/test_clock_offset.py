"""Clock offset estimation tests (Task 6.12; Requirements 9.8, 10.7; design D14).

**Validates: Requirements 9.8, 10.7**

Sign convention under test: `offset_ms = server_time - host_midpoint`; positive means the Collector clock is ahead of
the host. Corrected host time of a container timestamp is `ts - offset_ms` (the form `classify_b_samples` uses).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coroner.common.timeutil import format_utc_ms
from coroner.runner.collector_client import (
    ClockOffsetError,
    CollectorClient,
    HttpResponse,
    estimate_clock_offset_ms,
)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
BASE = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(ms: int) -> datetime:
    return BASE + timedelta(milliseconds=ms)


def stamp(ms: int) -> str:
    return format_utc_ms(at(ms))


def test_zero_offset() -> None:
    result = estimate_clock_offset_ms(at(0), at(100), stamp(50))
    assert result.offset_ms == 0
    assert result.uncertainty_ms == 50.0 and result.round_trip_ms == 100.0
    assert result.window_start == at(0) and result.window_end == at(100)


def test_known_positive_offset_collector_ahead() -> None:
    result = estimate_clock_offset_ms(at(0), at(100), stamp(50 + 3000))
    assert result.offset_ms == 3000
    corrected = at(3050) - timedelta(milliseconds=result.offset_ms)  # container time mapped to host time
    assert corrected == at(50)


def test_known_negative_offset_collector_behind() -> None:
    result = estimate_clock_offset_ms(at(1000), at(1020), stamp(1010 - 250))
    assert result.offset_ms == -250
    assert result.uncertainty_ms == 10.0


def test_zero_round_trip_has_zero_uncertainty() -> None:
    result = estimate_clock_offset_ms(at(5), at(5), stamp(7))
    assert result.offset_ms == 2 and result.uncertainty_ms == 0.0


def test_odd_round_trip_midpoint_rounds_to_nearest_ms() -> None:
    # midpoint is 0.5 ms into the window; server at 1 ms gives an exact 0.5 ms offset, rounded half to even
    assert estimate_clock_offset_ms(at(0), at(1), stamp(1)).offset_ms == 0
    assert estimate_clock_offset_ms(at(0), at(1), stamp(2)).offset_ms == 2  # 1.5 -> 2
    assert estimate_clock_offset_ms(at(0), at(3), stamp(0)).offset_ms == -2  # -1.5 -> -2


def test_accepts_aware_non_utc_host_times() -> None:
    plus5 = timezone(timedelta(hours=5))
    result = estimate_clock_offset_ms(at(0).astimezone(plus5), at(100).astimezone(plus5), stamp(60))
    assert result.offset_ms == 10


def test_as_dict_records_window_offset_and_uncertainty() -> None:
    data = estimate_clock_offset_ms(at(0), at(100), stamp(80)).as_dict()
    assert data["clock_offset_ms"] == 30 and data["uncertainty_ms"] == 50.0
    assert data["window_start"] == stamp(0) and data["window_end"] == stamp(100) and data["server_time"] == stamp(80)


@pytest.mark.parametrize(
    "server_time",
    ["", "yesterday", "2025-01-01T12:00:00Z", "2025-01-01T12:00:00.5Z", "2025-01-01T12:00:00.000+00:00",
     "2025-02-30T12:00:00.000Z", " 2025-01-01T12:00:00.000Z", None, 12345, 1.5, True, [], {}],
)
def test_invalid_or_missing_server_time_is_rejected(server_time: object) -> None:
    with pytest.raises(ClockOffsetError):
        estimate_clock_offset_ms(at(0), at(10), server_time)  # type: ignore[arg-type]


def test_non_monotonic_host_times_are_rejected() -> None:
    with pytest.raises(ClockOffsetError):
        estimate_clock_offset_ms(at(100), at(99), stamp(100))


def test_naive_host_times_are_rejected() -> None:
    with pytest.raises(ClockOffsetError):
        estimate_clock_offset_ms(datetime(2025, 1, 1), at(1), stamp(0))
    with pytest.raises(ClockOffsetError):
        estimate_clock_offset_ms(at(0), datetime(2025, 1, 1, 12, 0, 1), stamp(0))


def test_error_type_is_a_value_error() -> None:
    assert issubclass(ClockOffsetError, ValueError)


# ---- Property: independent integer-millisecond oracle ----

@settings(max_examples=300, deadline=None)
@given(
    start=st.integers(min_value=0, max_value=10**9),
    half_rtt=st.integers(min_value=0, max_value=5_000),
    offset=st.integers(min_value=-86_400_000, max_value=86_400_000),
)
def test_offset_matches_integer_oracle(start: int, half_rtt: int, offset: int) -> None:
    rtt = 2 * half_rtt  # even round trip: the midpoint is a whole millisecond, so the oracle is exact
    server_ms = start + half_rtt + offset  # construct the Collector clock from the known offset
    result = estimate_clock_offset_ms(at(start), at(start + rtt), stamp(server_ms))
    assert result.offset_ms == offset
    assert result.uncertainty_ms == half_rtt
    assert result.round_trip_ms == rtt
    # the true midpoint lies inside the window, and a container time maps back to host time within the uncertainty
    assert result.window_start <= at(start + half_rtt) <= result.window_end


# ---- Through the client (fake transport, fake wall clock) ----

def _client_offset(server_offset_ms: int, rtt_ms: int = 40) -> object:
    times = iter([at(0), at(rtt_ms)])
    body = ('{"status":"ok","storage_ok":true,"server_time":"%s"}' % stamp(rtt_ms // 2 + server_offset_ms)).encode()

    def transport(method: str, url: str, data: bytes | None, timeout_s: float) -> HttpResponse:
        return HttpResponse(200, body)

    client = CollectorClient("http://h", transport=transport, wall_clock=lambda: next(times))
    return client.health().clock_offset


@pytest.mark.parametrize("offset", [0, 3, -7, 120, -250])
def test_client_health_records_offset_and_window(offset: int) -> None:
    result = _client_offset(offset)
    assert result is not None and result.offset_ms == offset and result.uncertainty_ms == 20.0


def test_client_health_with_backwards_host_clock_has_no_offset() -> None:
    times = iter([at(100), at(50)])

    def transport(method: str, url: str, data: bytes | None, timeout_s: float) -> HttpResponse:
        return HttpResponse(200, ('{"storage_ok":true,"server_time":"%s"}' % stamp(75)).encode())

    health = CollectorClient("http://h", transport=transport, wall_clock=lambda: next(times)).health()
    assert health.clock_offset is None and health.clock_offset_error is not None and health.ok


# ---- Negative controls (fixtures only) ----

def _wrong_sign(host_before: datetime, host_after: datetime, server: datetime) -> int:
    midpoint = host_before + (host_after - host_before) / 2
    return round((midpoint - server) / timedelta(milliseconds=1))


def _endpoint_not_midpoint(host_before: datetime, host_after: datetime, server: datetime) -> int:
    return round((server - host_after) / timedelta(milliseconds=1))


def test_negative_control_flipped_sign_is_distinguishable() -> None:
    real = estimate_clock_offset_ms(at(0), at(100), stamp(3050))
    assert real.offset_ms == 3000
    assert _wrong_sign(at(0), at(100), at(3050)) == -3000


def test_negative_control_using_response_time_instead_of_midpoint_is_distinguishable() -> None:
    real = estimate_clock_offset_ms(at(0), at(100), stamp(50))
    assert real.offset_ms == 0
    assert _endpoint_not_midpoint(at(0), at(100), at(50)) == -50
