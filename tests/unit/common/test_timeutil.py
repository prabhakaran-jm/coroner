"""Tests for coroner.common.timeutil (Requirements 2.2, 4.2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coroner.common.timeutil import format_utc_ms, parse_utc_ms, utc_now_ms

UTC = timezone.utc
PLUS_2 = timezone(timedelta(hours=2))
MINUS_5_30 = timezone(timedelta(hours=-5, minutes=-30))


# --- format_utc_ms ----------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (datetime(2025, 1, 1, 12, 0, 0, 0, tzinfo=UTC), "2025-01-01T12:00:00.000Z"),
        (datetime(2025, 1, 1, 12, 0, 8, 31000, tzinfo=UTC), "2025-01-01T12:00:08.031Z"),
        (datetime(2025, 12, 31, 23, 59, 59, 999000, tzinfo=UTC), "2025-12-31T23:59:59.999Z"),
        (datetime(2024, 2, 29, 0, 0, 0, 1000, tzinfo=UTC), "2024-02-29T00:00:00.001Z"),
        (datetime(1, 1, 1, 0, 0, 0, 0, tzinfo=UTC), "0001-01-01T00:00:00.000Z"),
        (datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC), "9999-12-31T23:59:59.999Z"),
    ],
)
def test_format_known_values(value: datetime, expected: str) -> None:
    assert format_utc_ms(value) == expected


def test_format_truncates_sub_millisecond_digits_and_never_carries() -> None:
    assert format_utc_ms(datetime(2025, 1, 1, 12, 0, 59, 999999, tzinfo=UTC)) == "2025-01-01T12:00:59.999Z"
    assert format_utc_ms(datetime(2025, 1, 1, 12, 0, 0, 999, tzinfo=UTC)) == "2025-01-01T12:00:00.000Z"
    assert format_utc_ms(datetime(2025, 1, 1, 12, 0, 0, 1999, tzinfo=UTC)) == "2025-01-01T12:00:00.001Z"


@pytest.mark.parametrize(
    "value, expected",
    [
        (datetime(2025, 1, 1, 14, 0, 0, 5000, tzinfo=PLUS_2), "2025-01-01T12:00:00.005Z"),
        (datetime(2025, 1, 1, 0, 30, 0, 0, tzinfo=PLUS_2), "2024-12-31T22:30:00.000Z"),
        (datetime(2025, 1, 1, 6, 30, 1, 2000, tzinfo=MINUS_5_30), "2025-01-01T12:00:01.002Z"),
    ],
)
def test_format_converts_other_offsets_to_utc(value: datetime, expected: str) -> None:
    assert format_utc_ms(value) == expected


def test_format_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_utc_ms(datetime(2025, 1, 1, 12, 0, 0))


@pytest.mark.parametrize("value", ["2025-01-01T12:00:00.000Z", 1735732800, None])
def test_format_rejects_non_datetime(value: object) -> None:
    with pytest.raises(TypeError):
        format_utc_ms(value)  # type: ignore[arg-type]


def test_format_rejects_a_value_that_cannot_be_expressed_in_utc() -> None:
    with pytest.raises(ValueError, match="cannot be expressed in UTC"):
        format_utc_ms(datetime(1, 1, 1, 0, 0, 0, tzinfo=PLUS_2))


# --- parse_utc_ms -----------------------------------------------------------------


def test_parse_known_value_is_aware_utc() -> None:
    parsed = parse_utc_ms("2025-01-01T12:00:08.031Z")
    assert parsed == datetime(2025, 1, 1, 12, 0, 8, 31000, tzinfo=UTC)
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "text",
    [
        "2025-01-01T00:00:00.000Z",
        "2025-01-01T12:00:08.031Z",
        "2024-02-29T23:59:59.999Z",
        "0001-01-01T00:00:00.000Z",
        "9999-12-31T23:59:59.999Z",
        "2025-06-15T09:08:07.006Z",
    ],
)
def test_text_round_trips_exactly(text: str) -> None:
    assert format_utc_ms(parse_utc_ms(text)) == text


@pytest.mark.parametrize("micro", [0, 1, 999, 1000, 1001, 123456, 999000, 999999])
def test_datetime_round_trips_to_its_millisecond_truncation(micro: int) -> None:
    original = datetime(2025, 3, 4, 5, 6, 7, micro, tzinfo=UTC)
    back = parse_utc_ms(format_utc_ms(original))
    assert back == original.replace(microsecond=micro // 1000 * 1000)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "2025-01-01T12:00:00Z",  # no fraction
        "2025-01-01T12:00:00.0Z",  # one fractional digit
        "2025-01-01T12:00:00.00Z",
        "2025-01-01T12:00:00.0000Z",  # four fractional digits
        "2025-01-01T12:00:00.000000Z",
        "2025-01-01T12:00:00.000",  # no Z
        "2025-01-01T12:00:00.000z",  # lower-case z
        "2025-01-01T12:00:00.000+00:00",  # offset instead of Z
        "2025-01-01T12:00:00.000+02:00",
        "2025-01-01 12:00:00.000Z",  # space instead of T
        "2025-01-01t12:00:00.000Z",
        " 2025-01-01T12:00:00.000Z",
        "2025-01-01T12:00:00.000Z ",
        "2025-01-01T12:00:00.000Z\n",
        "25-01-01T12:00:00.000Z",
        "2025-1-1T12:00:00.000Z",
        "2025/01/01T12:00:00.000Z",
        "20250101T120000.000Z",
        "2025-01-01T12:00:00,000Z",  # comma decimal separator
        "２０２５-01-01T12:00:00.000Z",  # full-width digits
        "2025-01-01T12:00:00.000Z2025-01-01T12:00:00.000Z",
        "not a timestamp",
    ],
)
def test_parse_rejects_wrong_shape_and_names_the_value(text: str) -> None:
    with pytest.raises(ValueError) as info:
        parse_utc_ms(text)
    assert repr(text) in str(info.value)


@pytest.mark.parametrize(
    "text",
    [
        "2025-13-01T00:00:00.000Z",  # month 13
        "2025-00-10T00:00:00.000Z",  # month 0
        "2025-02-29T00:00:00.000Z",  # not a leap year
        "2100-02-29T00:00:00.000Z",  # century, not a leap year
        "2025-04-31T00:00:00.000Z",  # April has 30 days
        "2025-01-32T00:00:00.000Z",
        "2025-01-00T00:00:00.000Z",
        "2025-01-01T24:00:00.000Z",  # hour 24
        "2025-01-01T12:60:00.000Z",  # minute 60
        "2025-01-01T12:00:60.000Z",  # leap second is not representable
        "0000-01-01T00:00:00.000Z",  # year 0
    ],
)
def test_parse_rejects_impossible_calendar_values_and_names_the_value(text: str) -> None:
    with pytest.raises(ValueError) as info:
        parse_utc_ms(text)
    assert repr(text) in str(info.value)


def test_parse_accepts_leap_day_in_a_leap_year() -> None:
    assert parse_utc_ms("2024-02-29T12:00:00.000Z").day == 29
    assert parse_utc_ms("2000-02-29T12:00:00.000Z").day == 29


@pytest.mark.parametrize("value", [None, 1735732800, b"2025-01-01T12:00:00.000Z", datetime(2025, 1, 1, tzinfo=UTC)])
def test_parse_rejects_non_strings(value: object) -> None:
    with pytest.raises(TypeError):
        parse_utc_ms(value)  # type: ignore[arg-type]


# --- utc_now_ms -------------------------------------------------------------------


def test_now_default_clock_is_aware_utc_with_millisecond_precision() -> None:
    before = datetime.now(UTC) - timedelta(milliseconds=1)
    value = utc_now_ms()
    after = datetime.now(UTC) + timedelta(milliseconds=1)
    assert value.utcoffset() == timedelta(0)
    assert value.microsecond % 1000 == 0
    assert before <= value <= after
    assert parse_utc_ms(format_utc_ms(value)) == value


def test_now_truncates_the_injected_clock_to_milliseconds() -> None:
    fixed = datetime(2025, 1, 1, 12, 0, 0, 123999, tzinfo=UTC)
    assert utc_now_ms(lambda: fixed) == datetime(2025, 1, 1, 12, 0, 0, 123000, tzinfo=UTC)


def test_now_converts_an_injected_clock_in_another_offset_to_utc() -> None:
    value = utc_now_ms(lambda: datetime(2025, 1, 1, 14, 0, 0, 7500, tzinfo=PLUS_2))
    assert value == datetime(2025, 1, 1, 12, 0, 0, 7000, tzinfo=UTC)
    assert value.tzinfo is not None and value.utcoffset() == timedelta(0)


def test_now_rejects_a_naive_injected_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_now_ms(lambda: datetime(2025, 1, 1, 12, 0, 0))