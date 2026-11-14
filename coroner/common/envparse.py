"""Environment and size parsing helpers (Requirements 8.3, 8.4).

Standard library only. Every error names the offending value so a bad setting is easy
to find. `ConfigError` is a ValueError subclass; callers that need a process exit code
catch it at startup.
"""
from __future__ import annotations

import re

_INT_RE = re.compile(r"[+-]?[0-9]+")
_SIZE_RE = re.compile(r"([0-9]+)([bkmgBKMG]?)")
_UNIT_SHIFT = {"": 0, "b": 0, "k": 10, "m": 20, "g": 30}
_MAX_BYTES = 2**63 - 1  # Docker stores memory limits as a signed 64-bit integer


class ConfigError(ValueError):
    """A configuration value is invalid. The message names the setting and its value."""


def parse_int_env(
    name: str,
    raw: str | None,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Parse an integer setting.

    `raw` is the variable's value, for example `os.environ.get(name)`, so the function is
    pure. None, an empty string, and an all-whitespace string select `default`. Otherwise
    the value (surrounding whitespace ignored) must be an optionally signed ASCII integer
    within the inclusive bounds. Anything else raises ConfigError naming `name` and `raw`.
    """
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"minimum {minimum} is greater than maximum {maximum}")
    if not _in_bounds(default, minimum, maximum):
        raise ValueError(f"default {default} for {name} is not {_describe(minimum, maximum)}")
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip()
    try:
        number = int(text) if _INT_RE.fullmatch(text) else None
    except ValueError:  # an integer with an absurd number of digits
        number = None
    if number is None or not _in_bounds(number, minimum, maximum):
        raise ConfigError(f"invalid {name}={raw!r} (expected {_describe(minimum, maximum)})")
    return number


def parse_mem_size(text: str) -> int:
    """Parse a memory size such as `128m` into bytes.

    Grammar: ASCII digits followed by an optional unit `b`, `k`, `m`, or `g` (any case),
    with powers of 1024. A bare number is a byte count, as Docker reads it. No sign,
    decimal point, whitespace, or other suffix. The result must fit a signed 64-bit
    integer. Zero is accepted and returned; for Docker it means "no limit", so callers
    that need a real limit must check for it. Anything invalid raises ConfigError naming
    the value.
    """
    if not isinstance(text, str):
        raise TypeError(f"expected a str, got {type(text).__name__}")
    match = _SIZE_RE.fullmatch(text)
    if match is None:
        raise ConfigError(
            f"invalid memory size {text!r} (expected digits with an optional unit b, k, m or g, for example 128m)"
        )
    digits, unit = match.groups()
    size = int(digits) << _UNIT_SHIFT[unit.lower()]
    if size > _MAX_BYTES:
        raise ConfigError(f"invalid memory size {text!r} (larger than {_MAX_BYTES} bytes)")
    return size


def _in_bounds(value: int, minimum: int | None, maximum: int | None) -> bool:
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def _describe(minimum: int | None, maximum: int | None) -> str:
    if minimum is not None and maximum is not None:
        return f"an integer from {minimum} to {maximum}"
    if minimum is not None:
        return f"an integer of at least {minimum}"
    if maximum is not None:
        return f"an integer of at most {maximum}"
    return "an integer"