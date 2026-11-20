"""Tests for coroner.common.envparse (Requirements 8.3, 8.4)."""
from __future__ import annotations

import pytest

from coroner.common.envparse import ConfigError, parse_int_env, parse_mem_size

MAX_BYTES = 2**63 - 1


# --- parse_mem_size ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0", 0),
        ("0b", 0),
        ("0m", 0),
        ("1", 1),
        ("1b", 1),
        ("1B", 1),
        ("1k", 1024),
        ("1K", 1024),
        ("1m", 1024**2),
        ("1M", 1024**2),
        ("1g", 1024**3),
        ("1G", 1024**3),
        ("64m", 67108864),
        ("128m", 134217728),
        ("192m", 201326592),
        ("512k", 524288),
        ("2g", 2147483648),
        ("134217728", 134217728),
        ("0128m", 134217728),  # leading zeros are plain digits
        ("9223372036854775807", MAX_BYTES),
        ("9223372036854775807b", MAX_BYTES),
        ("8589934591g", 8589934591 * 1024**3),  # largest whole number of GiB that fits
    ],
)
def test_parse_mem_size_valid(text: str, expected: int) -> None:
    assert parse_mem_size(text) == expected


@pytest.mark.parametrize("unit, factor", [("b", 1), ("k", 1024), ("m", 1024**2), ("g", 1024**3)])
@pytest.mark.parametrize("number", [0, 1, 2, 9, 10, 63, 64, 127, 128, 1000, 4095, 65536])
@pytest.mark.parametrize("case", [str.lower, str.upper])
def test_parse_mem_size_is_number_times_power_of_1024(unit: str, factor: int, number: int, case) -> None:
    assert parse_mem_size(f"{number}{case(unit)}") == number * factor


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "m",
        "b",
        "g",
        " 128m",
        "128m ",
        "128 m",
        "\t128m",
        "128m\n",
        "-1m",
        "+1m",
        "-0",
        "1.5g",
        "1.g",
        ".5g",
        "1e3",
        "1,000k",
        "1_000k",
        "0x10",
        "0b101",
        "128x",
        "128t",  # Docker knows more units; the approved set is b, k, m, g
        "128kb",
        "128mb",
        "128gb",
        "128mm",
        "128 MiB",
        "128Mi",
        "one",
        "١٢٨m",  # Arabic-Indic digits
        "１２８m",  # full-width digits
        "128ｍ",  # full-width unit letter
        "128m128m",
        "9223372036854775808",  # 2**63, one past the largest signed 64-bit value
        "9223372036854775808b",
        "8589934592g",  # exactly 2**63 bytes
        "9" * 40,
        "99999999999999999999g",
    ],
)
def test_parse_mem_size_invalid_raises_config_error_naming_the_value(text: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_mem_size(text)
    assert repr(text) in str(info.value)
    assert isinstance(info.value, ValueError)


def test_parse_mem_size_error_message_says_what_is_accepted() -> None:
    with pytest.raises(ConfigError, match=r"invalid memory size '12q'.*128m"):
        parse_mem_size("12q")


@pytest.mark.parametrize("value", [None, 128, 1.5, b"128m", ["128m"]])
def test_parse_mem_size_rejects_non_strings(value: object) -> None:
    with pytest.raises(TypeError):
        parse_mem_size(value)  # type: ignore[arg-type]


def test_parse_mem_size_result_fits_the_docker_limit_exactly_at_the_boundary() -> None:
    assert parse_mem_size(str(MAX_BYTES)) == MAX_BYTES
    with pytest.raises(ConfigError):
        parse_mem_size(str(MAX_BYTES + 1))


# --- parse_int_env ----------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", " ", "   ", "\t", "\n"])
def test_unset_or_blank_selects_the_default(raw: str | None) -> None:
    assert parse_int_env("CORONER_SAMPLE_INTERVAL_MS", raw, default=500, minimum=100, maximum=60000) == 500


@pytest.mark.parametrize(
    "raw, expected",
    [("500", 500), ("0", 0), ("7", 7), (" 250 ", 250), ("\t300\n", 300), ("+42", 42), ("-3", -3), ("007", 7), ("100", 100)],
)
def test_valid_integers_without_bounds(raw: str, expected: int) -> None:
    assert parse_int_env("X", raw, default=1) == expected


def test_bounds_are_inclusive() -> None:
    assert parse_int_env("X", "100", default=500, minimum=100, maximum=60000) == 100
    assert parse_int_env("X", "60000", default=500, minimum=100, maximum=60000) == 60000
    with pytest.raises(ConfigError):
        parse_int_env("X", "99", default=500, minimum=100, maximum=60000)
    with pytest.raises(ConfigError):
        parse_int_env("X", "60001", default=500, minimum=100, maximum=60000)


def test_one_sided_bounds() -> None:
    assert parse_int_env("X", "5", default=10, minimum=5) == 5
    assert parse_int_env("X", "5", default=1, maximum=5) == 5
    with pytest.raises(ConfigError, match="at least 5"):
        parse_int_env("X", "4", default=10, minimum=5)
    with pytest.raises(ConfigError, match="at most 5"):
        parse_int_env("X", "6", default=1, maximum=5)


@pytest.mark.parametrize(
    "raw",
    ["abc", "1.5", "1.0", "1e3", "1_000", "0x10", "--1", "+-1", "1 2", "12abc", "٣", "１２", "nan", "inf", "True", "0b1", "1,5", "5.", "-"],
)
def test_invalid_integers_raise_config_error_naming_setting_and_value(raw: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_int_env("CORONER_SAMPLE_INTERVAL_MS", raw, default=500, minimum=100, maximum=60000)
    message = str(info.value)
    assert "CORONER_SAMPLE_INTERVAL_MS" in message
    assert repr(raw) in message
    assert "100 to 60000" in message


def test_out_of_range_message_names_the_value_and_the_range() -> None:
    with pytest.raises(ConfigError) as info:
        parse_int_env("CORONER_MAX_BODY", "70000", default=500, minimum=100, maximum=60000)
    assert str(info.value) == "invalid CORONER_MAX_BODY='70000' (expected an integer from 100 to 60000)"


def test_absurdly_long_integer_is_a_config_error_not_a_crash() -> None:
    with pytest.raises(ConfigError):
        parse_int_env("X", "9" * 5000, default=1, minimum=0, maximum=10)
    with pytest.raises(ConfigError):
        parse_int_env("X", "9" * 5000, default=1)


def test_the_default_is_not_validated_against_the_text_only_against_the_bounds() -> None:
    assert parse_int_env("X", None, default=100, minimum=100, maximum=100) == 100
    with pytest.raises(ValueError, match="default 5 for X"):
        parse_int_env("X", None, default=5, minimum=100)
    with pytest.raises(ValueError, match="minimum 10 is greater than maximum 5"):
        parse_int_env("X", "7", default=7, minimum=10, maximum=5)


def test_config_error_is_a_value_error() -> None:
    assert issubclass(ConfigError, ValueError)