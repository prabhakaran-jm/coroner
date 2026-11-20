"""UTC time helpers with millisecond precision (Requirements 2.2, 4.2).

The wire format is `YYYY-MM-DDTHH:mm:ss.sssZ`: UTC, exactly three fractional digits,
and a literal `Z`. Sub-millisecond digits are truncated, never rounded, so formatting
cannot carry into the next second. Standard library only.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone

_UTC_MS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{3})Z"
)
_EXPECTED = "YYYY-MM-DDTHH:mm:ss.sssZ"


def _truncate_to_ms_utc(value: datetime) -> datetime:
    """Return `value` as an aware UTC datetime with the microseconds cut to milliseconds."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"datetime must be timezone-aware, got naive value {value!r}")
    try:
        utc = value.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError(f"datetime {value!r} cannot be expressed in UTC") from exc
    return utc.replace(microsecond=utc.microsecond // 1000 * 1000)


def utc_now_ms(now: Callable[[], datetime] | None = None) -> datetime:
    """Current UTC time as an aware datetime truncated to milliseconds.

    `now` is an injectable clock for tests; it must return an aware datetime.
    """
    return _truncate_to_ms_utc((now or (lambda: datetime.now(timezone.utc)))())


def format_utc_ms(value: datetime) -> str:
    """Format an aware datetime as `YYYY-MM-DDTHH:mm:ss.sssZ` (converted to UTC, truncated to ms)."""
    u = _truncate_to_ms_utc(value)
    return (
        f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:{u.second:02d}"
        f".{u.microsecond // 1000:03d}Z"
    )


def parse_utc_ms(text: str) -> datetime:
    """Parse `YYYY-MM-DDTHH:mm:ss.sssZ` into an aware UTC datetime.

    Strict: any other shape (no fraction, other fraction length, an offset instead of `Z`,
    surrounding whitespace, non-ASCII digits) or an impossible calendar value raises
    ValueError naming the offending text.
    """
    if not isinstance(text, str):
        raise TypeError(f"expected a str, got {type(text).__name__}")
    match = _UTC_MS_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid UTC timestamp {text!r} (expected {_EXPECTED})")
    year, month, day, hour, minute, second, millis = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, millis * 1000, tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"invalid UTC timestamp {text!r}: {exc}") from exc