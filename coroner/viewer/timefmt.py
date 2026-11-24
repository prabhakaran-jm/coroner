"""Timestamp parsing and display for the Viewer.

Every displayed value comes from the public ``format_utc_ms`` (which truncates
sub-millisecond digits and never rounds). Strict manifest, Sample and gap times
use ``parse_utc_ms``. Raw Docker ``State.FinishedAt`` uses the local
``parse_docker_time``, a copy of the M0 rule pinned by a parity test.

This module never imports ``coroner.runner.experiments.base``, never uses the
private ``_fmt`` and never uses ``datetime.fromisoformat``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from coroner.common.timeutil import format_utc_ms, parse_utc_ms

__all__ = [
    "EPOCH",
    "display_utc_ms",
    "display_docker",
    "epoch_ms_utc",
    "epoch_ms_docker",
    "epoch_ms_to_text",
    "parse_docker_time",
]

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_MS = timedelta(milliseconds=1)

_DOCKER_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z"
)


def parse_docker_time(text: object) -> datetime | None:
    """Docker_Time_Parser: RFC 3339 UTC with 0 to 9 fractional digits and ``Z``.

    Anything else, and the zero value (year 1), gives ``None``.
    """
    if not isinstance(text, str):
        return None
    match = _DOCKER_TIME.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if year <= 1:
        return None
    micro = int(((match.group(7) or "") + "000000")[:6])
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_strict(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return parse_utc_ms(text)
    except (ValueError, TypeError):
        return None


def _format(value: datetime | None) -> str | None:
    if value is None:
        return None
    try:
        return format_utc_ms(value)
    except (ValueError, TypeError):
        return None


def _epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return (value - EPOCH) // _ONE_MS  # exact integer floor division


def display_utc_ms(text: object) -> str | None:
    """Strict ``YYYY-MM-DDTHH:mm:ss.sssZ`` text re-formatted; ``None`` on any failure."""
    return _format(_parse_strict(text))


def display_docker(text: object) -> str | None:
    """Docker timestamp formatted to milliseconds; ``None`` on failure or the zero value."""
    return _format(parse_docker_time(text))


def epoch_ms_utc(text: object) -> int | None:
    """Integer epoch milliseconds of a strict timestamp; ``None`` on any failure."""
    return _epoch_ms(_parse_strict(text))


def epoch_ms_docker(text: object) -> int | None:
    """Integer epoch milliseconds of a Docker timestamp, floored to the millisecond."""
    return _epoch_ms(parse_docker_time(text))


def epoch_ms_to_text(ms: int) -> str:
    """Format integer epoch milliseconds with exact integer arithmetic."""
    return format_utc_ms(EPOCH + timedelta(milliseconds=ms))
