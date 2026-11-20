"""Property 6: Durability configuration parsing (requirements 4.10, 4.11, 4.12, 13.14).

*For any* string value of `CORONER_DURABILITY`, the result is Crash_Durable when the value is
unset or empty, Process_Durable when it is exactly `process`, Crash_Durable when it is exactly
`crash`, and a configuration error that names the offending value in every other case.

**Validates: Requirements 4.10, 4.11, 4.12, 13.14**
"""
from __future__ import annotations

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from coroner.common.envparse import ConfigError
from coroner.evidence.writer import Durability, parse_durability

BOUNDARY_VALUES = [None, "", "process", "crash", "Crash", "CRASH", "Process", " process", "process ", "crash\n", " ", "\t", "0", "none"]

# Arbitrary text, None, and values built from the two valid words to land near the boundary.
RAW_VALUES = st.one_of(
    st.none(),
    st.text(),
    st.sampled_from(BOUNDARY_VALUES),
    st.builds(lambda a, b, c: a + b + c, st.sampled_from(["", " ", "\t", "\n", "x"]), st.sampled_from(["process", "crash", "Process", "Crash"]), st.sampled_from(["", " ", "\n", "x", "es"])),
    st.binary().map(lambda raw: raw.decode("latin-1")),
)


def expected(raw: str | None) -> Durability | None:
    """The property's oracle: None means a configuration error is required."""
    if raw is None or raw == "":
        return Durability.CRASH
    if raw == "process":
        return Durability.PROCESS
    if raw == "crash":
        return Durability.CRASH
    return None


@settings(max_examples=1000, deadline=None, derandomize=True, database=None)
@given(RAW_VALUES)
@example(None)
@example("")
@example("process")
@example("crash")
@example("Crash")
@example(" process")
def test_property_6_durability_configuration_parsing(raw: str | None) -> None:
    want = expected(raw)
    if want is None:
        with pytest.raises(ConfigError) as info:
            parse_durability(raw)
        assert str(info.value) == f"invalid CORONER_DURABILITY={raw!r} (expected 'process' or 'crash')"
        assert repr(raw) in str(info.value)  # the offending value is named
    else:
        result = parse_durability(raw)
        assert result is want and isinstance(result, Durability)


@settings(max_examples=300, deadline=None, derandomize=True, database=None)
@given(RAW_VALUES)
def test_property_6_parsing_is_total_and_only_ever_raises_config_error(raw: str | None) -> None:
    try:
        result = parse_durability(raw)
    except ConfigError:
        return
    assert result in (Durability.CRASH, Durability.PROCESS)


@pytest.mark.parametrize("raw, want", [(None, Durability.CRASH), ("", Durability.CRASH), ("process", Durability.PROCESS), ("crash", Durability.CRASH)])
def test_property_6_boundary_values_that_are_valid(raw: str | None, want: Durability) -> None:
    assert parse_durability(raw) is want


@pytest.mark.parametrize("raw", ["Crash", "CRASH", "Process", " process", "process ", "crash\n", " ", "\t", "0", "none"])
def test_property_6_boundary_values_that_are_invalid(raw: str) -> None:
    with pytest.raises(ConfigError, match="invalid CORONER_DURABILITY="):
        parse_durability(raw)


def test_property_6_the_default_with_nothing_configured_is_crash_durable() -> None:
    """Requirement 4.11: crash-durable is the M0 default."""
    assert parse_durability(None) is Durability.CRASH
    assert parse_durability("") is Durability.CRASH