"""Property 23: Memory size parsing.

**Validates: Requirements 8.3, 8.4, 11.12, 11.15**

The oracle below is independent of coroner.common.envparse: it uses a character scan and a
multiplication table instead of a regex and shifts.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from coroner.common.envparse import ConfigError
from coroner.runner.evaluate import parse_mem_size

MAX = 2**63 - 1
FACTOR = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
DIGITS = "0123456789"


def oracle(text):
    """Return the byte count, or None when `text` is not a valid size."""
    if not text:
        return None
    body = text
    unit = ""
    if text[-1] in "bkmgBKMG":
        body, unit = text[:-1], text[-1].lower()
    if not body or any(ch not in DIGITS for ch in body):
        return None
    value = 1 * int(body) * FACTOR[unit]
    return value if value <= MAX else None


@settings(max_examples=300, deadline=None)
@given(
    n=st.integers(min_value=0, max_value=2**32),
    unit=st.sampled_from(["", "b", "k", "m", "g"]),
    upper=st.booleans(),
    zeros=st.integers(min_value=0, max_value=3),
)
def test_valid_sizes_match_oracle(n, unit, upper, zeros):
    text = "0" * zeros + str(n) + (unit.upper() if upper else unit)
    expected = n * FACTOR[unit]
    assert oracle(text) == expected
    assert parse_mem_size(text) == expected


_INVALID_PARTS = st.one_of(
    st.text(max_size=12),
    st.builds(lambda n, s: str(n) + s, st.integers(0, 10**6), st.sampled_from(
        ["kb", "mb", "gb", "t", "T", "x", " ", "\n", ".5", "mm", "ib", "m ", "٣"])),
    st.builds(lambda n, p: p + str(n), st.integers(0, 10**6), st.sampled_from(
        ["-", "+", " ", "\t", "0x", "1.", ".", "m", "k"])),
)


@settings(max_examples=400, deadline=None)
@given(text=_INVALID_PARTS)
def test_arbitrary_strings_agree_with_oracle(text):
    expected = oracle(text)
    if expected is None:
        with pytest.raises(ConfigError) as exc:
            parse_mem_size(text)
        assert repr(text) in str(exc.value)
    else:
        assert parse_mem_size(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", " ", "m", "k", "-1m", "+1m", "1.5g", "1 m", " 1m", "1m ", "1m\n", "1kb", "1mb",
     "1t", "0x10", "1e3", "١٢٣", "1_000", "12m3", "mm", "1,000"],
)
def test_malformed_strings_raise_naming_value(text):
    assert oracle(text) is None
    with pytest.raises(ConfigError) as exc:
        parse_mem_size(text)
    assert isinstance(exc.value, ValueError)
    assert repr(text) in str(exc.value)


def test_default_128_mib_is_compose_default_bytes():
    assert parse_mem_size("128m") == 134217728
    assert parse_mem_size("128M") == 134217728
    assert parse_mem_size("134217728") == 134217728
    assert parse_mem_size("131072k") == 134217728
    assert parse_mem_size("134217728b") == 134217728
    assert oracle("128m") == 128 * 1024 * 1024


@pytest.mark.parametrize(
    "text, value",
    [("0", 0), ("0m", 0), ("1", 1), ("1b", 1), ("1k", 1024), ("1m", 1048576),
     ("1g", 1073741824), ("007k", 7168), (str(MAX), MAX), (str(MAX) + "b", MAX),
     ("8589934591g", 8589934591 * 1024**3)],
)
def test_boundary_values(text, value):
    assert parse_mem_size(text) == value == oracle(text)


@pytest.mark.parametrize(
    "text", [str(MAX + 1), str(MAX + 1) + "b", "8589934592g", "9007199254740992m", "9" * 40 + "k"]
)
def test_values_above_signed_64_bit_raise(text):
    assert oracle(text) is None
    with pytest.raises(ConfigError) as exc:
        parse_mem_size(text)
    assert repr(text) in str(exc.value)


@pytest.mark.parametrize("bad", [None, 128, 1.5, b"128m", ["128m"]])
def test_non_string_input_is_rejected(bad):
    with pytest.raises(TypeError):
        parse_mem_size(bad)


# --- isolated negative controls: in-memory mutants must disagree with the oracle ---

def _mutant_decimal_units(text):  # 1000-based units
    f = {"": 1, "b": 1, "k": 1000, "m": 10**6, "g": 10**9}
    return int(text[:-1] or "0") * f[text[-1].lower()] if text[-1:].lower() in "kmg" else int(text)


def _mutant_accepts_sign(text):
    return int(text.rstrip("bkmgBKMG")) * FACTOR[text[-1].lower() if text[-1:].isalpha() else ""]


def test_negative_control_oracle_detects_decimal_unit_mutant():
    assert _mutant_decimal_units("1k") != oracle("1k")
    assert _mutant_decimal_units("128m") != 134217728


def test_negative_control_oracle_rejects_what_sign_mutant_accepts():
    assert _mutant_accepts_sign("-1m") == -1048576
    assert oracle("-1m") is None
    with pytest.raises(ConfigError):
        parse_mem_size("-1m")
