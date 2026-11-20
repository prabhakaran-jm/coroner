"""Tests for coroner.collector.config (Requirements 4.10, 4.11, 4.12)."""
from __future__ import annotations

import copy
import dataclasses
import io
import os
import types
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from coroner.collector.config import (
    CollectorConfig,
    DEFAULT_EVIDENCE_DIR,
    load_config,
    load_config_or_exit,
)
from coroner.common.envparse import ConfigError
from coroner.evidence.writer import Durability


def test_empty_environment_gives_documented_defaults() -> None:
    cfg = load_config({})
    assert cfg == CollectorConfig(Durability.CRASH, 65536, 50, Path("/data/evidence"))
    assert DEFAULT_EVIDENCE_DIR == Path("/data/evidence")


def test_config_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        load_config({}).max_body_bytes = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("process", Durability.PROCESS), ("crash", Durability.CRASH), ("", Durability.CRASH)],
)
def test_durability_values(raw: str, expected: Durability) -> None:
    assert load_config({"CORONER_DURABILITY": raw}).durability is expected


@pytest.mark.parametrize("raw", ["Crash", "PROCESS", " crash", "crash ", "fast", "none", "0", "crash\n"])
def test_invalid_durability_names_the_value(raw: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_config({"CORONER_DURABILITY": raw})
    assert repr(raw) in str(info.value)
    assert "CORONER_DURABILITY" in str(info.value)


def test_exit_path_is_nonzero_and_names_value() -> None:
    err = io.StringIO()
    with pytest.raises(SystemExit) as info:
        load_config_or_exit({"CORONER_DURABILITY": "sloppy"}, stderr=err)
    assert info.value.code not in (0, None)
    assert "'sloppy'" in err.getvalue()


def test_exit_path_returns_config_when_valid() -> None:
    assert load_config_or_exit({"CORONER_DURABILITY": "process"}).durability is Durability.PROCESS


@pytest.mark.parametrize(
    ("var", "attr", "default"),
    [("CORONER_MAX_BODY_BYTES", "max_body_bytes", 65536), ("CORONER_WATCHDOG_TICK_MS", "watchdog_tick_ms", 50)],
)
class TestNumeric:
    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_uses_default(self, var: str, attr: str, default: int, raw: str) -> None:
        assert getattr(load_config({var: raw}), attr) == default

    @pytest.mark.parametrize(("raw", "value"), [("1", 1), ("99", 99), (" 12 ", 12), ("+5", 5)])
    def test_valid(self, var: str, attr: str, default: int, raw: str, value: int) -> None:
        assert getattr(load_config({var: raw}), attr) == value

    @pytest.mark.parametrize("raw", ["0", "-1", "1.5", "abc", "0x10", "1e3", "٣", "１２", "1_000", "5 5"])
    def test_rejected_not_defaulted(self, var: str, attr: str, default: int, raw: str) -> None:
        with pytest.raises(ConfigError) as info:
            load_config({var: raw})
        assert var in str(info.value)
        assert repr(raw) in str(info.value)


def test_body_bytes_has_no_upper_bound() -> None:
    assert load_config({"CORONER_MAX_BODY_BYTES": "1048576"}).max_body_bytes == 1048576
    assert load_config({"CORONER_MAX_BODY_BYTES": "100"}).max_body_bytes == 100


@pytest.mark.parametrize(("raw", "value"), [("1", 1), ("50", 50), ("99", 99)])
def test_watchdog_tick_valid_bounds(raw: str, value: int) -> None:
    assert load_config({"CORONER_WATCHDOG_TICK_MS": raw}).watchdog_tick_ms == value


@pytest.mark.parametrize("raw", ["0", "100", "101", "1048576"])
def test_watchdog_tick_out_of_range_names_variable_value_and_range(raw: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_config({"CORONER_WATCHDOG_TICK_MS": raw})
    message = str(info.value)
    assert "CORONER_WATCHDOG_TICK_MS" in message
    assert repr(raw) in message
    assert "from 1 to 99" in message


@given(st.integers(min_value=1, max_value=10**12), st.integers(min_value=1, max_value=99))
def test_numeric_round_trip(body: int, tick: int) -> None:
    cfg = load_config({"CORONER_MAX_BODY_BYTES": str(body), "CORONER_WATCHDOG_TICK_MS": str(tick)})
    assert (cfg.max_body_bytes, cfg.watchdog_tick_ms) == (body, tick)


def test_evidence_dir_override(tmp_path: Path) -> None:
    assert load_config({}, evidence_dir=tmp_path).evidence_dir == tmp_path
    cfg = load_config({}, evidence_dir=str(tmp_path))
    assert isinstance(cfg.evidence_dir, Path) and cfg.evidence_dir == tmp_path


def test_evidence_dir_is_not_read_from_environment() -> None:
    assert load_config({"CORONER_EVIDENCE_DIR": "/elsewhere"}).evidence_dir == DEFAULT_EVIDENCE_DIR


def test_deterministic_and_does_not_mutate_env() -> None:
    env = {"CORONER_DURABILITY": "process", "CORONER_MAX_BODY_BYTES": "10", "OTHER": "x"}
    before = copy.deepcopy(env)
    assert load_config(env) == load_config(env)
    assert env == before
    ro = types.MappingProxyType(env)
    assert load_config(ro).max_body_bytes == 10
    assert env == before


def test_does_not_read_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORONER_DURABILITY", "garbage")
    monkeypatch.setenv("CORONER_MAX_BODY_BYTES", "garbage")
    assert load_config({}) == CollectorConfig(Durability.CRASH, 65536, 50, DEFAULT_EVIDENCE_DIR)


def test_no_filesystem_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("filesystem access during load_config")

    for target, name in [(os, "makedirs"), (os, "mkdir"), (os, "open"), (os, "stat"), (os, "scandir"), (os, "listdir")]:
        monkeypatch.setattr(target, name, boom)
    monkeypatch.setattr(Path, "mkdir", boom)
    monkeypatch.setattr(Path, "open", boom)
    monkeypatch.setattr(Path, "exists", boom)
    monkeypatch.setattr(Path, "resolve", boom)
    monkeypatch.setattr("builtins.open", boom)
    load_config({"CORONER_DURABILITY": "process"}, evidence_dir=tmp_path / "missing")


def test_missing_directory_is_not_created(tmp_path: Path) -> None:
    target = tmp_path / "missing"
    load_config({}, evidence_dir=target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
